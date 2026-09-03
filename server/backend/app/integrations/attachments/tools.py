"""MCP tools for the attachments integration.

Exposes four tools:
  - attachments_scan:    populate pending rows from WhatsApp raw_json
  - attachments_pending: list unprocessed attachments with user-facing metadata
  - attachments_ingest:  download + parse + embed via Baileys bridge
  - attachments_search:  filename ILIKE lookup (any status)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.attachments.models import MessageAttachment
from app.integrations.attachments.scan import scan_gmail, scan_whatsapp
from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import iso_or_none, scoped_query, serialize


def _format_size(n: int | None) -> str:
    if n is None:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    return f"{n / (1024 * 1024):.1f} MB"


_ROW_FIELDS = ["id", "source", "filename", "mime_type", "sender_name", "chat_or_thread", "message_ts", "parse_status", "skip_reason"]
_ROW_RENAMES = {"sender_name": "sender", "chat_or_thread": "chat", "message_ts": "date", "parse_status": "status"}
_ROW_TRANSFORMS = {"message_ts": iso_or_none}


def _row(r: MessageAttachment, *, extra_fields: list[str] = ()) -> dict:
    """Mechanical field mapping shared by attachments_pending and attachments_search.
    `size` is a derived (not 1:1) field so it's merged in by hand."""
    out = serialize(r, _ROW_FIELDS + list(extra_fields), renames=_ROW_RENAMES, transforms=_ROW_TRANSFORMS)
    out["size"] = _format_size(r.size_bytes)
    return out


def attachments_scan_handler(session: Session, arguments: dict) -> str:
    wa = scan_whatsapp(session)
    gm = scan_gmail(session)
    return json.dumps({"whatsapp": wa, "gmail": gm})


def attachments_pending_handler(session: Session, arguments: dict) -> str:
    """List attachments awaiting the user's ingest decision.

    Optional filters: since_days (recency window), source, limit. Default is
    24 hours so daily-note can show 'new since yesterday' without overwhelming.
    """
    since_days = arguments.get("since_days")
    source = arguments.get("source")  # 'whatsapp' | 'gmail' | None
    limit = int(arguments.get("limit") or 50)
    status = arguments.get("status") or "pending"

    q = scoped_query(session, MessageAttachment).filter(
        MessageAttachment.parse_status == status
    )
    if source:
        q = q.filter(MessageAttachment.source == source)
    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(since_days))
        q = q.filter(MessageAttachment.message_ts >= cutoff)

    q = q.order_by(MessageAttachment.message_ts.desc().nullslast()).limit(limit)
    rows = q.all()

    return json.dumps({
        "count": len(rows),
        "status_filter": status,
        "results": [_row(r, extra_fields=["size_bytes"]) for r in rows],
    }, default=str)


def attachments_search_handler(session: Session, arguments: dict) -> str:
    """Filename ILIKE search across all attachment statuses.

    Useful when the user remembers a filename ("did anyone send the BoQ?")
    but doesn't know if it's been ingested yet. Cheap — runs against the
    `idx_msg_attachment_filename_search` index.
    """
    query = (arguments.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})
    limit = int(arguments.get("limit") or 25)

    pattern = f"%{escape_ilike(query)}%"
    rows = (
        scoped_query(session, MessageAttachment)
        .filter(MessageAttachment.filename.ilike(pattern, escape=ILIKE_ESCAPE_CHAR))
        .order_by(MessageAttachment.message_ts.desc().nullslast())
        .limit(limit)
        .all()
    )
    return json.dumps({
        "query": query,
        "count": len(rows),
        "results": [_row(r, extra_fields=["historical_doc_id"]) for r in rows],
    }, default=str)


def attachments_ingest_handler(session: Session, arguments: dict) -> str:
    """Download + parse + embed the given attachments.

    WhatsApp only today — bridge exposes /download/:message_id via Baileys'
    downloadMediaMessage. Each ingested attachment becomes a HistoricalDocument
    and is surfaced by `corpus_search` automatically.
    """
    from app.integrations.attachments.ingest import ingest_many

    raw_ids = arguments.get("ids") or []
    try:
        ids = [int(i) for i in raw_ids]
    except (TypeError, ValueError) as e:
        return json.dumps({"status": "error", "detail": f"ids must be integers: {e}"})
    if not ids:
        return json.dumps({"status": "error", "detail": "no ids provided"})

    result = ingest_many(session, ids)
    return json.dumps(result, default=str)


def mcp_tools() -> list[dict[str, Any]]:
    return [
        CustomTool(
            name="attachments_scan",
            description=(
                "Scan WhatsApp and Gmail for unprocessed attachments. "
                "Populates metadata-only rows in message_attachments — no "
                "downloads happen here. Safe to call repeatedly; idempotent "
                "via the (source, message_ref, filename) unique constraint. "
                "WhatsApp scans instantly from already-cached message data. "
                "Gmail does a bounded, checkpointed full-format backfill "
                "against the live API each call (new attachments since the "
                "last scan, plus one page further into history) — repeated "
                "calls make progress until history is fully covered."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=attachments_scan_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="attachments_pending",
            description=(
                "List attachments awaiting user approval. Use before "
                "attachments_ingest so the user can see filenames, sizes, "
                "senders, and dates and pick which to ingest. Supports "
                "filtering by source and recency. Covers both "
                "source='whatsapp' and source='gmail' rows once "
                "attachments_scan has run — note attachments_ingest only "
                "downloads WhatsApp today, so Gmail rows are queued as "
                "'unsupported' (not 'pending') and won't show under the "
                "default pending filter; pass status='unsupported' to see them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["whatsapp", "gmail"],
                        "description": "Restrict to one source, optional.",
                    },
                    "since_days": {
                        "type": "integer",
                        "description": "Only show attachments from the last N days.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "ingested", "skipped", "failed", "unsupported"],
                        "default": "pending",
                    },
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                },
            },
            handler=attachments_pending_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="attachments_search",
            description=(
                "Filename search across all attachments (any status). Use "
                "when the user remembers part of a filename. Returns "
                "metadata + parse_status so caller can tell whether the "
                "doc is already ingested or still pending."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring to match in filename."},
                    "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 200},
                },
                "required": ["query"],
            },
            handler=attachments_search_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="attachments_ingest",
            description=(
                "Download, parse, and embed a specific set of attachments "
                "into the historical corpus. WhatsApp documents only. After "
                "ingest the doc is searchable via corpus_search with "
                "source_type=wa_attachment_{pdf,docx,xlsx}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "message_attachments.id values to ingest.",
                    },
                },
                "required": ["ids"],
            },
            handler=attachments_ingest_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
    ]
