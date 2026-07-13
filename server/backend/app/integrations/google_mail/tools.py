"""MCP tool definitions and handlers for Gmail.

Mix of DSL-built tools (recent, search, semantic_search, stats) and
hand-written CustomTool wrappers for the bespoke ones (unread/thread
both reach the live Gmail API; backfill + embed are admin operations).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.google_mail.models import MailMessage
from app.tools import CustomTool, ExtraFilter, ListTool, SearchTool, SemanticSearchTool, StatsTool
from app.tools.helpers import iso_or_none, make_enrich, scoped_query, serialize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MSG_FIELDS = ["google_message_id", "thread_id", "account_email", "subject", "sender", "to", "date", "snippet", "is_read", "is_starred", "has_attachments"]
_MSG_RENAMES = {"google_message_id": "id", "account_email": "account", "sender": "from"}
_MSG_TRANSFORMS = {"date": iso_or_none}


def _msg_to_dict(m: MailMessage) -> dict:
    return serialize(m, _MSG_FIELDS, renames=_MSG_RENAMES, transforms=_MSG_TRANSFORMS)


# ---------------------------------------------------------------------------
# Hand-written handlers (unread + thread hit the live API; backfill/embed are admin)
# ---------------------------------------------------------------------------

def handle_unread(session: Session, arguments: dict[str, Any]) -> str:
    """Unread messages across all accounts. Live Gmail API with DB cache fallback."""
    account = arguments.get("account")
    limit = min(int(arguments.get("limit", 20)), 100)
    uid = current_user_id()

    try:
        from app.integrations.google_mail.client import list_messages
        from app.models.tokens import OAuthToken

        # Scope enumeration to the requesting user — OAuthToken is per-user
        # and listing across users would leak Sam's mail to Alex (and v.v.).
        token_q = (
            session.query(OAuthToken.account_email)
            .filter(OAuthToken.provider == "google", OAuthToken.user_id == uid)
        )
        if account:
            # Caller-supplied account must belong to the requesting user.
            owned = token_q.filter(OAuthToken.account_email == account).first()
            if not owned:
                return json.dumps({"error": f"account {account} not owned by current user"})
            accounts = [account]
        else:
            accounts = [t.account_email for t in token_q.distinct().all()]

        all_messages = []
        for acct in accounts:
            msgs = list_messages(
                acct, session,
                user_id=uid, query="is:unread", label_ids=["INBOX"], max_results=limit,
            )
            all_messages.extend(msgs)

        all_messages.sort(key=lambda m: m.get("date") or "", reverse=True)
        all_messages = all_messages[:limit]

        results = [
            {
                "id": m.get("google_message_id"),
                "thread_id": m.get("thread_id"),
                "account": m.get("account_email"),
                "subject": m.get("subject"),
                "from": m.get("sender"),
                "to": m.get("to"),
                "date": m.get("date"),
                "snippet": m.get("snippet"),
                "is_read": m.get("is_read", False),
                "is_starred": m.get("is_starred", False),
                "has_attachments": m.get("has_attachments", False),
            }
            for m in all_messages
        ]
        return json.dumps(results, indent=2)

    except Exception:
        logger.exception("Live Gmail fetch failed, falling back to cache")

    # Fallback to DB cache
    query = scoped_query(session, MailMessage).filter(MailMessage.is_read == False)  # noqa: E712
    if account:
        query = query.filter(MailMessage.account_email == account)
    query = query.order_by(MailMessage.date.desc().nullslast())
    return json.dumps([_msg_to_dict(m) for m in query.limit(limit).all()], indent=2)


def handle_thread(session: Session, arguments: dict[str, Any]) -> str:
    """Get all messages in a thread. Live API for full bodies; cache fallback."""
    thread_id = arguments.get("thread_id", "").strip()
    if not thread_id:
        return json.dumps({"error": "thread_id is required"})

    messages = (
        scoped_query(session, MailMessage)
        .filter_by(thread_id=thread_id)
        .order_by(MailMessage.date.asc())
        .all()
    )
    if not messages:
        return json.dumps({"error": f"Thread {thread_id} not found"})

    account = messages[0].account_email
    uid = current_user_id()
    try:
        from app.integrations.google_mail.client import get_thread, get_message
        from app.db import get_db
        db = get_db()
        with db.session() as api_session:
            thread_msgs = get_thread(account, api_session, thread_id, user_id=uid)
            if thread_msgs:
                result = []
                for msg in thread_msgs:
                    full = get_message(
                        account, api_session, msg["google_message_id"], user_id=uid,
                    )
                    if full:
                        result.append({
                            "subject": full["subject"],
                            "from": full["sender"],
                            "to": full["to"],
                            "date": full["date"],
                            "body": full.get("body", ""),
                            "snippet": full["snippet"],
                        })
                    else:
                        result.append({
                            "subject": msg["subject"],
                            "from": msg["sender"],
                            "date": msg["date"],
                            "snippet": msg["snippet"],
                        })
                return json.dumps(result, indent=2)
    except Exception:
        logger.exception(f"Failed to fetch thread {thread_id} from API, using cached data")

    return json.dumps([_msg_to_dict(m) for m in messages], indent=2)


def handle_backfill(session: Session, arguments: dict[str, Any]) -> str:
    """Backfill Gmail history for an account (scoped to the requesting user)."""
    account = arguments.get("account", "").strip()
    if not account:
        return json.dumps({"error": "account is required"})

    uid = current_user_id()

    # Verify the requested account belongs to the requesting user.
    from app.models.tokens import OAuthToken
    owned = (
        session.query(OAuthToken)
        .filter_by(user_id=uid, provider="google", account_email=account)
        .first()
    )
    if not owned:
        return json.dumps({"error": f"account {account} not owned by current user"})

    after_date = arguments.get("after_date", "2021/01/01").strip()
    from app.integrations.google_mail.sync import backfill_mail
    try:
        count = backfill_mail(account, session, user_id=uid, after_date=after_date)
        total = scoped_query(session, MailMessage).filter_by(account_email=account).count()
        return json.dumps({
            "status": "ok",
            "new_messages": count,
            "total_cached": total,
            "query": f"after:{after_date}",
        })
    except Exception as e:
        logger.exception(f"Backfill failed for {account}")
        return json.dumps({"error": str(e)})


def handle_embed(session: Session, arguments: dict[str, Any]) -> str:
    """Enqueue un-embedded mail messages for semantic search."""
    from app.integrations.google_mail.sync import embed_messages
    from app.services.embedding import Embedding
    try:
        count = embed_messages(session)
        total = session.query(Embedding).filter_by(source="email").count()
        return json.dumps({
            "status": "ok",
            "new_enqueued": count,
            "total_embedded": total,
        })
    except Exception as e:
        logger.exception("Embedding enqueue failed")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# DSL: SemanticSearchTool enrich + StatsTool compute callbacks
# ---------------------------------------------------------------------------

_semantic_enrich = make_enrich(
    model=MailMessage,
    id_column="google_message_id",
    fields=["thread_id", "subject", "sender", "to", "date"],
    renames={"sender": "from"},
    transforms={"date": iso_or_none},
)


def _gmail_stats_compute(session: Session, _arguments: dict[str, Any]) -> dict:
    """Per-account counts + embedding coverage. Scoped to the bearer —
    StatsTool compute callbacks bypass the DSL auto-scoping, so the
    user_id filters here are load-bearing (caught by the scoping suite)."""
    from sqlalchemy import func
    from app.services.embedding import Embedding, EmbeddingQueue

    uid = current_user_id()
    account_stats = (
        session.query(
            MailMessage.account_email,
            func.count(MailMessage.id),
            func.min(MailMessage.date),
            func.max(MailMessage.date),
        )
        .filter(MailMessage.user_id == uid)
        .group_by(MailMessage.account_email)
        .all()
    )
    total_embedded = (
        session.query(func.count(Embedding.id))
        .filter_by(source="email", user_id=uid).scalar() or 0
    )
    total_messages = sum(row[1] for row in account_stats)
    queue_pending = (
        session.query(func.count(EmbeddingQueue.id))
        .filter_by(source="email", status="pending", user_id=uid).scalar() or 0
    )
    accounts = [
        {
            "account": email,
            "message_count": count,
            "earliest": earliest.isoformat() if earliest else None,
            "latest": latest.isoformat() if latest else None,
        }
        for email, count, earliest, latest in account_stats
    ]
    return {
        "total_messages": total_messages,
        "total_embedded": total_embedded,
        "queue_pending": queue_pending,
        "embedding_coverage": round(total_embedded / total_messages * 100, 1) if total_messages else 0,
        "accounts": accounts,
    }


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        # ---- Hand-written: live Gmail API + admin operations ----
        CustomTool(
            name="gmail_unread",
            description=(
                "List unread Gmail messages across all accounts. Returns subject, sender, "
                "date, and a snippet preview. Fetches live from the Gmail API for freshness."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account email to filter by (optional)."},
                    "limit": {"type": "integer", "description": "Max results (default 20, max 100).", "default": 20},
                },
            },
            handler=handle_unread,
            category="email",
            examples=["What's in my inbox?", "Show unread emails from work"],
        ).build(),

        CustomTool(
            name="gmail_thread",
            description=(
                "Read all messages in a Gmail thread by thread_id. Includes full bodies "
                "where available (live API) or cached snippets as a fallback. "
                "Use this after finding a message via search or unread to read the actual conversation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "The Gmail thread ID."},
                },
                "required": ["thread_id"],
            },
            handler=handle_thread,
            category="email",
            examples=["Read this email thread", "Show me the full conversation"],
        ).build(),

        CustomTool(
            name="gmail_backfill",
            description=(
                "Backfill Gmail history from a given date. Fetches all messages and caches "
                "metadata. Admin tool — use for initial setup or catching up on history."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account email to backfill."},
                    "after_date": {
                        "type": "string",
                        "description": "Fetch messages after this date (YYYY/MM/DD). Default: 2021/01/01.",
                        "default": "2021/01/01",
                    },
                },
                "required": ["account"],
            },
            handler=handle_backfill,
            category="email",
        ).build(),

        CustomTool(
            name="gmail_embed",
            description=(
                "Embed un-embedded mail messages for semantic search. Fetches full bodies "
                "and creates vector embeddings. Admin tool — run after backfill or periodically."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_embed,
            category="email",
        ).build(),

        # ---- DSL: cached reads + stats + semantic search ----
        ListTool(
            name="gmail_recent",
            description=(
                "Recent cached email messages, optionally filtered by account or date range. "
                "Reads from the local cache (fast). Use gmail_unread for live inbox state."
            ),
            model=MailMessage,
            timestamp_col="date",
            to_dict=_msg_to_dict,
            default_limit=20,
            max_limit=500,
            extra_filters=[
                ExtraFilter(
                    param_name="account",
                    column="account_email",
                    description="Account email to filter by (optional).",
                    match_mode="exact",
                ),
            ],
            category="email",
            examples=["Recent emails this week", "Last 50 messages from work account"],
        ).build(),

        SearchTool(
            name="gmail_search",
            description=(
                "Keyword search across cached email subject/sender/snippet. Multi-term: "
                "every term must match somewhere. For meaning-based search use gmail_semantic_search."
            ),
            model=MailMessage,
            search_columns=["subject", "sender", "snippet"],
            timestamp_col="date",
            to_dict=_msg_to_dict,
            default_limit=20,
            max_limit=500,
            extra_filters=[
                ExtraFilter(
                    param_name="account",
                    column="account_email",
                    description="Account email to filter by (optional).",
                    match_mode="exact",
                ),
            ],
            category="email",
            examples=["Search emails for 'mortgage'", "Find emails from school about Finn"],
        ).build(),

        SemanticSearchTool(
            name="gmail_semantic_search",
            description=(
                "Semantic search across email content using embeddings (meaning-based, not keyword). "
                "Searches full message bodies. Great for finding emails about a topic even when "
                "you don't remember the exact words. Requires gmail_embed to have been run first."
            ),
            model=MailMessage,
            embedding_source="email",
            embed_tool_name="gmail_embed",
            enrich=_semantic_enrich,
            default_limit=10,
            max_limit=50,
            category="search",
            examples=[
                "Find emails about holiday planning",
                "What did we discuss about the mortgage?",
            ],
        ).build(),

        StatsTool(
            name="gmail_stats",
            description=(
                "Email cache statistics: per-account message counts, date ranges covered, "
                "and embedding coverage percentage. Use to check how much history is indexed."
            ),
            model=MailMessage,
            compute=_gmail_stats_compute,
            input_schema={"type": "object", "properties": {}},
            category="email",
        ).build(),
    ]
