"""MCP tools for the media store.

  - media_recent: list indexed/stored media with filters (chat, caption, type…)
  - media_sync:   scan whatsapp_messages + auto-download the recent window
  - media_fetch:  download specific items by id (works on 'expired'/'failed' too)
  - media_export: copy stored items into the vault with readable filenames,
                  for embedding in notes (snag evidence, receipts, etc.)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.media.models import MediaItem
from app.integrations.media.scan import scan_whatsapp_media
from app.integrations.media import store
from app.tools.helpers import iso_or_none, scoped_query, serialize


def _row(r: MediaItem) -> dict:
    return serialize(
        r,
        [
            "id", "source", "media_type", "mime_type", "size_bytes", "caption",
            "sender_name", "chat_or_thread", "message_ts", "status", "storage_path",
            "skip_reason",
        ],
        renames={"sender_name": "sender", "chat_or_thread": "chat", "message_ts": "date"},
        transforms={"message_ts": iso_or_none},
    )


def _apply_filters(q, arguments: dict):
    if arguments.get("media_type"):
        q = q.filter(MediaItem.media_type == arguments["media_type"])
    if arguments.get("status"):
        q = q.filter(MediaItem.status == arguments["status"])
    if arguments.get("chat"):
        q = q.filter(MediaItem.chat_or_thread.ilike(f"%{arguments['chat']}%"))
    if arguments.get("sender"):
        q = q.filter(MediaItem.sender_name.ilike(f"%{arguments['sender']}%"))
    if arguments.get("caption_contains"):
        q = q.filter(MediaItem.caption.ilike(f"%{arguments['caption_contains']}%"))
    if arguments.get("since_days") is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(arguments["since_days"]))
        q = q.filter(MediaItem.message_ts >= cutoff)
    return q


def media_recent_handler(session: Session, arguments: dict) -> str:
    limit = min(int(arguments.get("limit") or 50), 500)
    q = _apply_filters(scoped_query(session, MediaItem), arguments)
    rows = q.order_by(MediaItem.message_ts.desc().nullslast()).limit(limit).all()
    return json.dumps({"count": len(rows), "results": [_row(r) for r in rows]}, default=str)


def media_sync_handler(session: Session, arguments: dict) -> str:
    scan = scan_whatsapp_media(session)
    downloaded = store.download_pending(session)
    return json.dumps({"scan": scan, "download": downloaded})


def media_fetch_handler(session: Session, arguments: dict) -> str:
    try:
        ids = [int(i) for i in (arguments.get("ids") or [])]
    except (TypeError, ValueError) as e:
        return json.dumps({"status": "error", "detail": f"ids must be integers: {e}"})
    if not ids:
        return json.dumps({"status": "error", "detail": "no ids provided"})

    items = scoped_query(session, MediaItem).filter(MediaItem.id.in_(ids)).all()
    results = []
    for item in items:
        if item.status == "stored" and item.storage_path:
            results.append({"id": item.id, "status": "stored", "storage_path": item.storage_path})
            continue
        ok = store.download_item(session, item)
        session.commit()
        results.append({
            "id": item.id,
            "status": item.status,
            "storage_path": item.storage_path,
            "skip_reason": item.skip_reason if not ok else None,
        })
    missing = sorted(set(ids) - {i.id for i in items})
    return json.dumps({"results": results, "not_found": missing})


def media_export_handler(session: Session, arguments: dict) -> str:
    """Copy stored items into the vault. Select by ids OR by the same filters
    media_recent takes. Items not yet stored are downloaded first."""
    from app.services.vault_paths import resolve

    dest_folder = (arguments.get("dest_folder") or "").strip()
    if not dest_folder:
        return json.dumps({"status": "error", "detail": "dest_folder is required (vault-relative)"})
    try:
        vault_dir = resolve(dest_folder)
    except ValueError as e:
        return json.dumps({"status": "error", "detail": str(e)})

    ids = arguments.get("ids") or []
    if ids:
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError) as e:
            return json.dumps({"status": "error", "detail": f"ids must be integers: {e}"})
        items = scoped_query(session, MediaItem).filter(MediaItem.id.in_(ids)).all()
    else:
        limit = min(int(arguments.get("limit") or 100), 500)
        q = _apply_filters(scoped_query(session, MediaItem), arguments)
        items = q.order_by(MediaItem.message_ts.asc()).limit(limit).all()

    if not items:
        return json.dumps({"status": "error", "detail": "no matching media items"})

    exported, failed = [], []
    for item in items:
        if item.status != "stored":
            store.download_item(session, item)
            session.commit()
        try:
            dest = store.export_to_vault(item, vault_dir)
        except (ValueError, FileNotFoundError, OSError) as e:
            failed.append({"id": item.id, "caption": item.caption, "error": str(e)})
            continue
        exported.append({
            "id": item.id,
            "caption": item.caption,
            "date": item.message_ts.isoformat() if item.message_ts else None,
            "vault_path": f"{dest_folder.rstrip('/')}/{dest.name}",
        })
    return json.dumps({
        "exported": len(exported),
        "failed": len(failed),
        "dest_folder": dest_folder,
        "files": exported,
        "errors": failed,
    })


def mcp_tools() -> list[dict[str, Any]]:
    _filters = {
        "media_type": {"type": "string", "enum": ["image", "video", "audio"]},
        "status": {"type": "string", "enum": ["indexed", "stored", "expired", "failed"]},
        "chat": {"type": "string", "description": "Substring match on chat/group name."},
        "sender": {"type": "string", "description": "Substring match on sender name."},
        "caption_contains": {
            "type": "string",
            "description": "Substring match on the media caption (e.g. 'Snag -').",
        },
        "since_days": {"type": "integer", "description": "Only items from the last N days."},
    }
    return [
        {
            "name": "media_recent",
            "description": (
                "List WhatsApp media (images/videos/audio) from the media store "
                "index, newest first. Filter by chat, sender, caption substring, "
                "type, status, or recency. status='stored' means the bytes are "
                "on disk and exportable to the vault."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_filters,
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                },
            },
            "handler": media_recent_handler,
        },
        {
            "name": "media_sync",
            "description": (
                "Scan WhatsApp messages for new media and auto-download the "
                "recent window (last ~30 days) into the media store. Runs on a "
                "schedule anyway — call this to pick up something sent minutes ago."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "handler": media_sync_handler,
        },
        {
            "name": "media_fetch",
            "description": (
                "Force-download specific media items by id, including 'expired' "
                "or previously 'failed' ones (WhatsApp re-upload is attempted; "
                "old items may be genuinely unrecoverable)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "media_items.id values to download.",
                    },
                },
                "required": ["ids"],
            },
            "handler": media_fetch_handler,
        },
        {
            "name": "media_export",
            "description": (
                "Copy stored media into a vault folder with readable filenames "
                "(HHMM-caption-slug.ext) so notes can embed them — snag evidence, "
                "receipts, etc. Select by ids or by the media_recent filters. "
                "Returns vault-relative paths ready for wiki-linking."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "dest_folder": {
                        "type": "string",
                        "description": (
                            "Vault-relative destination folder, e.g. "
                            "'Attachments/Snags 2026-07-07 WindowCo'. Created if missing."
                        ),
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Explicit media_items.id values (skips filters).",
                    },
                    **_filters,
                    "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                },
                "required": ["dest_folder"],
            },
            "handler": media_export_handler,
        },
    ]
