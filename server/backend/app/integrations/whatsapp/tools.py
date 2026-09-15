"""MCP tool definitions and handlers for WhatsApp.

DSL-built: recent, search, semantic_search, stats.
Hand-written (CustomTool): thread (chat-by-id), contacts (different model), embed (admin).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.whatsapp.models import WhatsAppMessage, WhatsAppContact
from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike
from app.tools import CustomTool, ExtraFilter, ListTool, SearchTool, SemanticSearchTool, StatsTool, ToolAnnotations
from app.tools.helpers import make_enrich, scoped_query, serialize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg_to_dict(m: WhatsAppMessage) -> dict:
    return {
        "message_id": m.message_id,
        "chat_id": m.chat_id,
        "chat_name": m.chat_name,
        "sender": m.sender_name or m.sender_id,
        "is_group": m.is_group,
        "date": m.timestamp.isoformat() if m.timestamp else None,
        "type": m.message_type,
        "body": m.body,
        "caption": m.media_caption,
        "is_from_me": m.is_from_me,
    }


def _chat_filter(session: Session, query, value: str):
    """Filter messages by contact/group name. Resolves via WhatsAppContact then chat_id."""
    if not value:
        return query
    pattern = f"%{escape_ilike(value)}%"
    # Scoped: `whatsapp_contacts` became per-user when the second bridge
    # landed. Resolving a name against every user's contacts would let one
    # person's chat names steer another person's message filter.
    matching_contacts = (
        scoped_query(session, WhatsAppContact)
        .with_entities(WhatsAppContact.jid)
        .filter(
            WhatsAppContact.name.ilike(pattern, escape=ILIKE_ESCAPE_CHAR)
            | WhatsAppContact.notify_name.ilike(pattern, escape=ILIKE_ESCAPE_CHAR)
            | WhatsAppContact.jid.ilike(pattern, escape=ILIKE_ESCAPE_CHAR)
        )
        .all()
    )
    jids = [c.jid for c in matching_contacts]
    if jids:
        return query.filter(WhatsAppMessage.chat_id.in_(jids))
    # Fallback: direct ILIKE on chat_id
    return query.filter(WhatsAppMessage.chat_id.ilike(pattern, escape=ILIKE_ESCAPE_CHAR))


def _include_groups_filter(session: Session, query, value: bool):
    """If False, exclude group messages."""
    if value is False:  # explicit False; default True is no-op
        return query.filter(WhatsAppMessage.is_group == False)  # noqa: E712
    return query


# ---------------------------------------------------------------------------
# Hand-written handlers (thread, contacts, embed)
# ---------------------------------------------------------------------------

def handle_thread(session: Session, arguments: dict[str, Any]) -> str:
    chat_id = arguments.get("chat_id", "").strip()
    if not chat_id:
        return json.dumps({"error": "chat_id is required"})
    limit = min(int(arguments.get("limit", 50)), 200)

    messages = (
        scoped_query(session, WhatsAppMessage)
        .filter_by(chat_id=chat_id)
        .order_by(WhatsAppMessage.timestamp.desc())
        .limit(limit)
        .all()
    )
    if not messages:
        return json.dumps({"error": f"No messages found for chat {chat_id}"})

    messages.reverse()  # chronological for the model
    return json.dumps([_msg_to_dict(m) for m in messages], indent=2)


def handle_contacts(session: Session, arguments: dict[str, Any]) -> str:
    include_groups = arguments.get("include_groups", True)
    limit = min(int(arguments.get("limit", 30)), 100)

    query = scoped_query(session, WhatsAppContact)
    if not include_groups:
        query = query.filter(WhatsAppContact.is_group == False)  # noqa: E712

    contacts = query.order_by(WhatsAppContact.last_message_at.desc().nullslast()).limit(limit).all()

    return json.dumps([
        {
            "jid": c.jid,
            "name": c.name or c.notify_name or c.jid,
            "is_group": c.is_group,
            "last_message": c.last_message_at.isoformat() if c.last_message_at else None,
        }
        for c in contacts
    ], indent=2)


def handle_embed(session: Session, arguments: dict[str, Any]) -> str:
    from app.integrations.whatsapp.sync import embed_messages
    from app.services.embedding import Embedding
    try:
        count = embed_messages(session, user_id=current_user_id())
        total = session.query(Embedding).filter_by(source="whatsapp").count()
        return json.dumps({"status": "ok", "new_enqueued": count, "total_embedded": total})
    except Exception as e:
        logger.exception("WhatsApp embedding enqueue failed")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# DSL: SemanticSearch enrich + Stats compute
# ---------------------------------------------------------------------------

_semantic_enrich = make_enrich(
    id_key="segment_id",
    metadata_fields=["chat_id", "chat_name", "is_group", "start", "end", "message_count", "participants"],
    preview_key="conversation",
    preview_len=500,
)


def _semantic_date_filter(after_dt: datetime | None, before_dt: datetime | None):
    """Build an embeddings-table filter clause for whatsapp_semantic_search's
    after/before args.

    A WhatsApp embedding row is a conversation *segment* (several messages
    merged into one chunk, see sync.py's `_build_segments`), not a single
    `WhatsAppMessage` row — there is no natural join key back to one message,
    unlike gmail's google_message_id. The segment's real start time is
    already carried in `metadata_json["start"]` (an ISO string, set by
    `_segment_metadata` at enqueue time) purely for exactly this reason, so
    filtering casts that JSON field to a timestamp rather than using
    `Embedding.created_at` (embedding time, not message time — the same trap
    noted in google_mail's `_semantic_date_filter`). Cast happens inside the
    WHERE clause `EmbeddingService.search()` already builds before its
    `ORDER BY`/`LIMIT`, so this narrows the top-K candidate set in SQL rather
    than trimming results after the fact.
    """
    from sqlalchemy import DateTime, and_, cast
    from sqlalchemy.dialects.postgresql import JSONB

    from app.services.embedding import Embedding

    start_expr = cast(
        cast(Embedding.metadata_json, JSONB)["start"].astext,
        DateTime(timezone=True),
    )
    conditions = []
    if after_dt is not None:
        conditions.append(start_expr >= after_dt)
    if before_dt is not None:
        conditions.append(start_expr <= before_dt)
    return and_(*conditions)


def _whatsapp_stats_compute(session: Session, _arguments: dict[str, Any]) -> dict:
    """Per-caller stats.

    Every figure here is scoped to `current_user_id()`. Unscoped these are a
    metadata leak rather than a content one — total volume, group count and the
    date of someone's earliest message describe a person's WhatsApp life even
    though no message body is returned — and they're simply wrong for the
    caller, who sees a total that isn't theirs.
    """
    from app.services.embedding import Embedding, EmbeddingQueue

    user_id = current_user_id()

    total_messages = (
        session.query(func.count(WhatsAppMessage.id))
        .filter(WhatsAppMessage.user_id == user_id).scalar() or 0
    )
    total_contacts = (
        session.query(func.count(WhatsAppContact.id))
        .filter(WhatsAppContact.user_id == user_id).scalar() or 0
    )
    total_groups = (
        session.query(func.count(WhatsAppContact.id))
        .filter(WhatsAppContact.user_id == user_id, WhatsAppContact.is_group.is_(True))
        .scalar() or 0
    )
    # `embeddings` carries the owning user_id (see sync.py's per-(user, chat)
    # windowing), so these two scope the same way.
    total_embedded = (
        session.query(func.count(Embedding.id))
        .filter(Embedding.source == "whatsapp", Embedding.user_id == user_id)
        .scalar() or 0
    )
    queue_pending = (
        session.query(func.count(EmbeddingQueue.id))
        .filter(
            EmbeddingQueue.source == "whatsapp",
            EmbeddingQueue.status == "pending",
            EmbeddingQueue.user_id == user_id,
        )
        .scalar() or 0
    )
    earliest = (
        session.query(func.min(WhatsAppMessage.timestamp))
        .filter(WhatsAppMessage.user_id == user_id).scalar()
    )
    latest = (
        session.query(func.max(WhatsAppMessage.timestamp))
        .filter(WhatsAppMessage.user_id == user_id).scalar()
    )
    return {
        "total_messages": total_messages,
        "total_contacts": total_contacts,
        "total_groups": total_groups,
        "total_embedded": total_embedded,
        "queue_pending": queue_pending,
        "earliest": earliest.isoformat() if earliest else None,
        "latest": latest.isoformat() if latest else None,
    }


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def get_mcp_tools() -> list[dict]:
    return [
        ListTool(
            name="whatsapp_recent",
            description=(
                "Recent WhatsApp messages across all chats. Filter by contact or group "
                "name to focus on a specific conversation. Use this to catch up."
            ),
            model=WhatsAppMessage,
            timestamp_col="timestamp",
            to_dict=_msg_to_dict,
            default_limit=30,
            max_limit=100,
            extra_filters=[
                ExtraFilter(
                    param_name="chat",
                    column="chat_id",
                    description="Filter by contact or group name (resolved via WhatsAppContact).",
                    match_mode=_chat_filter,
                ),
                ExtraFilter(
                    param_name="include_groups",
                    column="is_group",
                    description="Include group messages (default true).",
                    param_type="boolean",
                    default=True,
                    match_mode=_include_groups_filter,
                ),
            ],
            category="home",
            examples=[
                "What's been said on WhatsApp today?",
                "Recent messages from a given contact",
                "Show group chat messages",
            ],
        ).build(),

        SearchTool(
            name="whatsapp_search",
            description=(
                "Keyword search across WhatsApp message bodies and media captions. "
                "For meaning-based search, use whatsapp_semantic_search instead."
            ),
            model=WhatsAppMessage,
            search_columns=["body", "media_caption"],
            timestamp_col="timestamp",
            to_dict=_msg_to_dict,
            default_limit=20,
            max_limit=100,
            extra_filters=[
                ExtraFilter(
                    param_name="chat",
                    column="chat_id",
                    description="Filter to a specific contact or group (optional).",
                    match_mode=_chat_filter,
                ),
            ],
            category="search",
            examples=["Search WhatsApp for plumber", "Find messages about school pickup"],
        ).build(),

        CustomTool(
            name="whatsapp_thread",
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            description=(
                "Read full message history for a specific WhatsApp chat by chat_id (JID). "
                "Returns messages in chronological order. Use after whatsapp_contacts to "
                "get the chat_id for a specific person or group."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "The WhatsApp chat JID (e.g. 353851234567@s.whatsapp.net or 120363012345@g.us).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 50, max 200).",
                        "default": 50,
                    },
                },
                "required": ["chat_id"],
            },
            handler=handle_thread,
            category="home",
        ).build(),

        CustomTool(
            name="whatsapp_contacts",
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            description=(
                "List WhatsApp contacts and groups, sorted by most recent message. "
                "Shows name, JID (needed for whatsapp_thread), and last message time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_groups": {
                        "type": "boolean",
                        "description": "Include groups (default true).",
                        "default": True,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max contacts (default 30, max 100).",
                        "default": 30,
                    },
                },
            },
            handler=handle_contacts,
            category="home",
            examples=["Who have I been chatting with?", "List WhatsApp contacts"],
        ).build(),

        SemanticSearchTool(
            name="whatsapp_semantic_search",
            description=(
                "Semantic search across WhatsApp conversations using embeddings. "
                "Returns conversation segments (groups of messages in context) ranked "
                "by relevance. Better than keyword search for topics and discussions. "
                "Optional after/before date range narrows to segments that actually "
                "started in that window — useful when the same topic recurs over "
                "years and only a specific period's conversations matter. "
                "Requires whatsapp_embed to have been run."
            ),
            model=WhatsAppMessage,
            embedding_source="whatsapp",
            embed_tool_name="whatsapp_embed",
            enrich=_semantic_enrich,
            date_filter=_semantic_date_filter,
            default_limit=10,
            max_limit=50,
            category="search",
            examples=[
                "Find WhatsApp conversations about holiday plans",
                "What was discussed about the birthday party?",
            ],
        ).build(),

        CustomTool(
            name="whatsapp_embed",
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            description=(
                "Embed un-embedded WhatsApp messages for semantic search. "
                "Admin tool — run after initial setup or periodically to index new messages."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_embed,
            category="home",
        ).build(),

        StatsTool(
            name="whatsapp_stats",
            description=(
                "WhatsApp statistics: total messages, contacts, groups, embedding coverage, "
                "and date range of captured messages. Admin tool for checking data health."
            ),
            model=WhatsAppMessage,
            compute=_whatsapp_stats_compute,
            input_schema={"type": "object", "properties": {}},
            category="home",
        ).build(),
    ]
