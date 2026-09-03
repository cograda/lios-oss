"""google_mail's declared facade — capability `mail.query` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.google_mail`. Consumers:
  - `system` (unread count + semantic search for the morning briefing /
    search-everything composites)
  - `attachments` (paging `has:attachment` candidates and full-format
    fetching for its own ingest pipeline)
  - `app/routes/integrations.py` (the dashboard's manual backfill/embed
    admin endpoints — V4 chunk 4.3e; these two are a fixed 1:1 dependency
    with no other consumer, so no `provides`/`depends_on` capability
    entries were added for them, per `app.plugin.capabilities`'s own
    docstring on when a direct facade import is fine without going through
    `get_capability()`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.integrations.google_mail import client as _client
from app.integrations.google_mail import sync as _sync
from app.integrations.google_mail.models import MailMessage
from app.integrations.google_mail.tools import _semantic_date_filter, get_mcp_tools, handle_unread
from app.tools.helpers import handler_for

# `import client as _client` + `_client.fn(...)`, not `from client import fn`
# — the latter copies a reference at import time, so tests that monkeypatch
# `app.integrations.google_mail.client.fn` directly (the normal pattern in
# this codebase) would silently patch a binding nothing here still points
# to. Module-attribute access resolves at call time instead.

# gmail_semantic_search is DSL-built (SemanticSearchTool) rather than a
# standalone `handle_*` function — its handler only exists as the closure
# `SemanticSearchTool.build()` returns, so it's looked up once here by tool
# name rather than duplicated.
_TOOLS = get_mcp_tools()
_SEMANTIC_SEARCH_HANDLER = handler_for(_TOOLS, "gmail_semantic_search")
_RECENT_HANDLER = handler_for(_TOOLS, "gmail_recent")


class GoogleMailFacade:
    def unread(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_unread(session, arguments)

    def recent(self, session: Session, arguments: dict[str, Any]) -> str:
        """Recent messages across the lookback window (DSL `ListTool`)."""
        return _RECENT_HANDLER(session, arguments)

    def semantic_search(self, session: Session, arguments: dict[str, Any]) -> str:
        return _SEMANTIC_SEARCH_HANDLER(session, arguments)

    def date_filter_clause(self, after_dt, before_dt):
        """Embeddings-table filter clause for a mail date range — see
        `google_mail.tools._semantic_date_filter` docstring. Exposed for
        `search_semantic`'s cross-source date filtering."""
        return _semantic_date_filter(after_dt, before_dt)

    def list_attachment_candidates_page(
        self,
        account_email: str,
        session: Session,
        *,
        user_id: int,
        page_token: str | None = None,
        max_results: int = 100,
    ) -> tuple[list[str], str | None]:
        return _client.list_attachment_candidates_page(
            account_email, session, user_id=user_id, page_token=page_token, max_results=max_results,
        )

    def fetch_messages_full(
        self, account_email: str, session: Session, message_ids: list[str], *, user_id: int,
    ) -> list[dict]:
        return _client.fetch_messages_full(account_email, session, message_ids, user_id=user_id)

    def backfill(
        self, account_email: str, session: Session, *, user_id: int, after_date: str,
    ) -> int:
        return _sync.backfill_mail(account_email, session, user_id=user_id, after_date=after_date)

    def embed_messages(self, session: Session) -> int:
        return _sync.embed_messages(session)

    def embedding_text(
        self, session: Session, google_message_id: str, user_id: int | None
    ) -> str | None:
        """Rebuild a message's embeddable text from local rows.

        Exists because `embeddings.chunk_text` is NOT authoritative for mail.
        `import_timemachine_mail.py` writes Embedding rows directly, bypassing
        the enqueue chokepoint, and stores `body[:4000]` — **body only, with no
        subject**. Re-offering that text would silently drop the subject from
        all 24,493 mail chunks, measured at -34.4% on 10-NN agreement, which is
        several times larger than everything cleaning buys.

        Bare subject + body, matching `sync.embed_messages` exactly, so a chunk
        rebuilt here is indistinguishable from one the live path produced.

        Reads `body_text` locally rather than calling the Gmail API: 126,654 of
        127,019 messages carry a stored body, and a re-clean pass is not worth
        127k API round-trips. The 365 without one fall back to their snippet —
        degraded but present, and re-fetchable later if it ever matters.
        """
        msg = (
            session.query(MailMessage)
            .filter(MailMessage.google_message_id == google_message_id)
            .filter(MailMessage.user_id == user_id)
            .first()
        )
        if msg is None:
            return None
        body = msg.body_text or msg.snippet or ""
        return f"{msg.subject or ''}\n\n{body}".strip() or None

    def has_data(self, session: Session, user_id: int) -> bool:
        """Cheap presence check — used by `app.mcp.instructions` to decide
        whether to offer this integration in a user's personalized render
        (sam-rollout D1)."""
        count = (
            session.query(func.count(MailMessage.id))
            .filter(MailMessage.user_id == user_id)
            .scalar()
        )
        return bool(count)


FACADE = GoogleMailFacade()
