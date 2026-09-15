"""whatsapp's declared facade — capability `whatsapp.query` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.whatsapp`. Two consumers: `system`'s search-everything
composite, and `inbox`, which pulls message-to-self notes into the triage queue.

⚠️ **The inbox dependency points this way for a reason, and must not be
inverted.** The intuitive shape — whatsapp pushing notes into the inbox — closes
a capability cycle that `app/plugin/validate.py` rejects at boot:

    whatsapp -> inbox.ingest -> notify.push -> system.alerts -> whatsapp.query

`system` composes nearly every read facade, including this one, so anything
`system` can reach must not depend on `inbox`. The inbox pulling from here is
also the better design independently: the inbox already owns a cron whose whole
job is drawing items into the queue, exactly as `attachments` pulls from
`mail.query`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.integrations.whatsapp.models import WhatsAppMessage
from app.integrations.whatsapp.tools import _semantic_date_filter, get_mcp_tools
from app.tools.helpers import handler_for

# Both of these are DSL-built (SemanticSearchTool / ListTool), not standalone
# `handle_*` functions — see `handler_for`'s docstring for why the built tool
# dict is the only place those closures exist.
_TOOLS = get_mcp_tools()
_SEMANTIC_SEARCH_HANDLER = handler_for(_TOOLS, "whatsapp_semantic_search")
_RECENT_HANDLER = handler_for(_TOOLS, "whatsapp_recent")


class WhatsAppFacade:
    def semantic_search(self, session: Session, arguments: dict[str, Any]) -> str:
        return _SEMANTIC_SEARCH_HANDLER(session, arguments)

    def date_filter_clause(self, after_dt, before_dt):
        """Embeddings-table filter clause for a WhatsApp date range — see
        `whatsapp.tools._semantic_date_filter` docstring. Exposed for
        `search_semantic`'s cross-source date filtering."""
        return _semantic_date_filter(after_dt, before_dt)

    def recent(self, session: Session, arguments: dict[str, Any]) -> str:
        """Recent messages across all chats (DSL `ListTool`)."""
        return _RECENT_HANDLER(session, arguments)

    def self_chat_map(self) -> dict[int, str]:
        """`{user_id: jid}` for configured message-to-self chats.

        Exposed rather than letting a consumer read `plugin_config("whatsapp")`
        itself: which chats are notebooks is this integration's knowledge, and a
        consumer reaching into another integration's config keys would couple it
        to a schema it doesn't own.
        """
        from app.integrations.whatsapp.sync import self_chat_map

        return self_chat_map()

    def self_notes(
        self, session: Session, *, limit: int = 100, user_id: int | None = None,
        since: Any = None,
    ) -> list[dict[str, Any]]:
        """Notes users wrote to themselves, oldest first, as plain dicts.

        Returns typed data rather than the rendered JSON strings the tool
        handlers above produce — a consumer routing these into a queue needs the
        message id and timestamp as values, not a formatted blob to re-parse.

        ⚠️ **Matches on the (user_id, jid) pair AND `is_from_me`.** A WhatsApp
        `@lid` is scoped to the account that observed it, so filtering on JID
        alone returns other people's messages: Alex's self-chat LID matches 178
        rows under Sam's bridge, all of them *received* from a third party. That
        shipped for one afternoon and put 59 of her messages into her inbox as her
        own notes. The `is_from_me` clause is the belt to the pair's braces — in a
        real self-chat every message is from you, so a received message proves the
        JID means something else here.

        **Deliberately not scoped to `current_user_id()`.** The only caller is a
        cron sweep with no request user bound, and each returned row carries its
        own `user_id` so the caller writes into the right person's tree. Pass
        `user_id` to narrow it; every consumer must then honour the field rather
        than assuming one owner.

        Oldest first so a backlog is drained in the order it was captured, and so
        a `limit` truncates the newest rather than stranding the oldest forever.

        `since` bounds how far back to look. The policy is the caller's, not this
        integration's — the inbox wants recent notes because a triage queue is for
        things still worth acting on, while the embedding pass deliberately wants
        every note ever written.
        """
        from sqlalchemy import and_, or_

        self_map = self.self_chat_map()
        if not self_map:
            return []

        pairs = [
            and_(WhatsAppMessage.user_id == uid, WhatsAppMessage.chat_id == jid)
            for uid, jid in self_map.items()
            if user_id is None or uid == user_id
        ]
        if not pairs:
            return []

        q = (
            session.query(WhatsAppMessage)
            .filter(or_(*pairs))
            .filter(WhatsAppMessage.is_from_me.is_(True))
            .filter(WhatsAppMessage.body.isnot(None))
        )
        if since is not None:
            q = q.filter(WhatsAppMessage.timestamp >= since)

        rows = q.order_by(WhatsAppMessage.timestamp).limit(limit).all()
        return [
            {
                "message_id": m.message_id,
                "user_id": m.user_id,
                "chat_id": m.chat_id,
                "body": m.body,
                "timestamp": m.timestamp,
                "message_type": m.message_type,
            }
            for m in rows
        ]

    def has_data(self, session: Session, user_id: int) -> bool:
        """Cheap presence check — used by `app.mcp.instructions` to decide
        whether to offer this integration in a user's personalized render
        (sam-rollout D1)."""
        count = (
            session.query(func.count(WhatsAppMessage.id))
            .filter(WhatsAppMessage.user_id == user_id)
            .scalar()
        )
        return bool(count)

    def contact_names(self, session: Session, *, user_id: int) -> dict[str, str]:
        """`{jid: display_name}` for every one of `user_id`'s own contacts
        and groups (`name` first, falling back to `notify_name`).

        `tasks_intake_candidates` (lios#151) uses this to fix a reported bug:
        candidates showed the raw chat JID rather than the contact/group
        name. `WhatsAppMessage.chat_name` (what `whatsapp_recent`'s
        `_msg_to_dict` reports) is populated by the bridge for groups but is
        routinely null for 1:1 chats, so falling back to `chat_id` there
        showed the JID for exactly the messages a person is most likely to
        recognise by name. One query for the whole caller's contact list,
        same batching shape as `_load_whatsapp_segments` in `tasks/intake.py`
        — never one lookup per candidate message.
        """
        from app.integrations.whatsapp.models import WhatsAppContact

        rows = (
            session.query(WhatsAppContact.jid, WhatsAppContact.name, WhatsAppContact.notify_name)
            .filter(WhatsAppContact.user_id == user_id)
            .all()
        )
        out: dict[str, str] = {}
        for jid, name, notify_name in rows:
            resolved = name or notify_name
            if resolved:
                out[jid] = resolved
        return out


FACADE = WhatsAppFacade()
