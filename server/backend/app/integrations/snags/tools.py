"""MCP tools for the snag register. The DB is the source of truth; every
write re-renders the vault view (Household/Renovation/Snags.md).

  - snag_capture: parse 'Snag - …' WhatsApp messages into snags (idempotent)
  - snag_list:    filter by status/trade/room/severity/text
  - snag_add:     manual entry (walkthroughs, phone calls)
  - snag_update:  triage by UID — status, trade, severity, notes, external ref
  - snag_render:  force a re-render of the vault note
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.integrations.snags.capture import capture_whatsapp_snags, _next_uid
from app.integrations.snags.models import (
    SEVERITIES, STATUSES, TRADES, Snag, SnagMedia,
)
from app.integrations.snags.render import OPEN_STATUSES, render_snags_note
from app.tools.helpers import iso_or_none, serialize

_ROW_FIELDS = [
    "uid", "title", "room", "element", "trade", "severity", "status",
    "reported_by", "reported_at", "external_ref", "resolution_note",
]


def _row(s: Snag, media_count: int | None = None) -> dict:
    out = serialize(s, _ROW_FIELDS, transforms={"reported_at": iso_or_none})
    out["media_count"] = media_count
    return out


def _render(session: Session) -> str:
    return render_snags_note(session)


def snag_capture_handler(session: Session, arguments: dict) -> str:
    since_days = int(arguments.get("since_days") or 7)
    result = capture_whatsapp_snags(session, since_days=since_days)
    if result["snags_created"]:
        result["rendered"] = _render(session)
    return json.dumps(result)


def snag_list_handler(session: Session, arguments: dict) -> str:
    q = session.query(Snag)
    if arguments.get("status"):
        q = q.filter(Snag.status == arguments["status"])
    elif not arguments.get("include_closed"):
        q = q.filter(Snag.status.in_(OPEN_STATUSES))
    if arguments.get("trade"):
        q = q.filter(Snag.trade == arguments["trade"])
    if arguments.get("room"):
        q = q.filter(Snag.room.ilike(f"%{arguments['room']}%"))
    if arguments.get("severity"):
        q = q.filter(Snag.severity == arguments["severity"])
    if arguments.get("query"):
        pat = f"%{arguments['query']}%"
        q = q.filter(Snag.title.ilike(pat) | Snag.description.ilike(pat))
    limit = min(int(arguments.get("limit") or 100), 500)
    rows = q.order_by(Snag.id).limit(limit).all()

    media_counts = dict(
        session.query(SnagMedia.snag_id, sa_func.count(SnagMedia.id))
        .filter(SnagMedia.snag_id.in_([r.id for r in rows] or [0]))
        .group_by(SnagMedia.snag_id)
        .all()
    )
    return json.dumps({
        "count": len(rows),
        "results": [_row(s, media_counts.get(s.id, 0)) for s in rows],
    }, default=str)


def snag_add_handler(session: Session, arguments: dict) -> str:
    title = (arguments.get("title") or "").strip()
    room = (arguments.get("room") or "").strip()
    if not title or not room:
        return json.dumps({"status": "error", "detail": "title and room are required"})

    snag = Snag(
        uid=_next_uid(session),
        title=title[:300],
        description=arguments.get("description") or title,
        room=room,
        element=arguments.get("element"),
        trade=arguments.get("trade") or "unknown",
        severity=arguments.get("severity") or "minor",
        reported_by=arguments.get("reported_by"),
        reported_at=datetime.now(timezone.utc),
    )
    session.add(snag)
    session.commit()
    rendered = _render(session)
    return json.dumps({"created": _row(snag), "rendered": rendered})


def snag_update_handler(session: Session, arguments: dict) -> str:
    uid = (arguments.get("uid") or "").strip().upper()
    snag = session.query(Snag).filter(Snag.uid == uid).one_or_none()
    if not snag:
        return json.dumps({"status": "error", "detail": f"no snag with uid {uid!r}"})

    changed = {}
    for field, allowed in (
        ("status", STATUSES), ("trade", TRADES), ("severity", SEVERITIES),
    ):
        if arguments.get(field):
            val = arguments[field]
            if val not in allowed:
                return json.dumps({
                    "status": "error",
                    "detail": f"{field} must be one of {list(allowed)}",
                })
            setattr(snag, field, val)
            changed[field] = val
    for field in ("title", "description", "room", "element", "external_ref", "resolution_note"):
        if arguments.get(field) is not None:
            setattr(snag, field, arguments[field])
            changed[field] = arguments[field]

    if arguments.get("attach_media_ids"):
        # Link media store items as evidence (e.g. photos re-sent after the
        # original message was unusable). Render exports them to the vault.
        from app.integrations.media.models import MediaItem

        attached = []
        for mid in arguments["attach_media_ids"]:
            # Deliberately unscoped: MediaItem is UserOwnedMixin, but the snag
            # register is a household-shared resource — any family member's
            # WhatsApp media may be attached as evidence here, not just the
            # current user's own items.
            item = session.get(MediaItem, int(mid))
            if item is None:
                return json.dumps({"status": "error", "detail": f"no media item with id {mid}"})
            exists = (
                session.query(SnagMedia)
                .filter(SnagMedia.snag_id == snag.id, SnagMedia.media_item_id == item.id)
                .one_or_none()
            )
            if not exists:
                session.add(SnagMedia(snag_id=snag.id, media_item_id=item.id))
                attached.append(item.id)
        changed["attach_media_ids"] = attached

    if arguments.get("remove_media"):
        # Detach ALL evidence from this snag (mis-linked photo correction).
        # Exported vault copies are deleted so the next render is clean;
        # the underlying media_items rows are untouched.
        from app.services.vault_paths import resolve

        removed = []
        for sm in session.query(SnagMedia).filter(SnagMedia.snag_id == snag.id).all():
            if sm.vault_path:
                try:
                    resolve(sm.vault_path).unlink(missing_ok=True)
                except Exception:
                    pass
                removed.append(sm.vault_path)
            session.delete(sm)
        changed["remove_media"] = removed

    if changed.get("status") == "reported" and not snag.reported_to_trade_at:
        snag.reported_to_trade_at = datetime.now(timezone.utc)
    if changed.get("status") in ("fixed", "verified", "closed", "wont-fix") and not snag.resolved_at:
        snag.resolved_at = datetime.now(timezone.utc)

    session.commit()
    rendered = _render(session)
    return json.dumps({"updated": _row(snag), "changed": changed, "rendered": rendered})


def snag_render_handler(session: Session, arguments: dict) -> str:
    return json.dumps({"rendered": _render(session)})


def mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "snag_capture",
            "description": (
                "Scan WhatsApp for structured snag reports ('Snag - room - trade "
                "- detail' texts or photo captions) and register them in the snag "
                "database with UIDs, linking evidence photos from the media store. "
                "Idempotent — already-captured messages are skipped. Re-renders "
                "the vault Snags note."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since_days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
                },
            },
            "handler": snag_capture_handler,
        },
        {
            "name": "snag_list",
            "description": (
                "List snags from the register (open ones by default). Filter by "
                "status, trade, room, severity, or free-text. Each snag has a "
                "stable UID (SNAG-0042) for tracking with trades."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "include_closed": {"type": "boolean", "default": False},
                    "trade": {"type": "string", "enum": list(TRADES)},
                    "room": {"type": "string"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "query": {"type": "string", "description": "Substring in title/description."},
                    "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                },
            },
            "handler": snag_list_handler,
        },
        {
            "name": "snag_add",
            "description": (
                "Manually add a snag (walkthrough findings, phone reports). "
                "Assigns the next UID and re-renders the vault note."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "room": {"type": "string"},
                    "description": {"type": "string"},
                    "element": {"type": "string"},
                    "trade": {"type": "string", "enum": list(TRADES)},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "reported_by": {"type": "string"},
                },
                "required": ["title", "room"],
            },
            "handler": snag_add_handler,
        },
        {
            "name": "snag_update",
            "description": (
                "Triage/update a snag by UID: reassign trade, set severity, move "
                "status through the lifecycle (open → reported → accepted/disputed "
                "→ fixed → verified → closed, or wont-fix), attach the trade's "
                "ticket number, or record a resolution note. Sets "
                "reported_to_trade_at / resolved_at automatically. Re-renders."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "e.g. SNAG-0042"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "trade": {"type": "string", "enum": list(TRADES)},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "room": {"type": "string"},
                    "element": {"type": "string"},
                    "external_ref": {"type": "string"},
                    "resolution_note": {"type": "string"},
                    "attach_media_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Media store item ids (from media_recent) to link as evidence. Exported to the vault on render.",
                    },
                    "remove_media": {
                        "type": "boolean",
                        "description": "Detach ALL evidence photos from this snag and delete their exported vault copies (for mis-linked photos). Re-attach correct ones via snag_capture or a fresh export.",
                    },
                },
                "required": ["uid"],
            },
            "handler": snag_update_handler,
        },
        {
            "name": "snag_render",
            "description": (
                "Force a re-render of the generated vault note "
                "(Household/Renovation/Snags.md) from the snag database, "
                "exporting any missing evidence photos to Attachments/Snags/."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "handler": snag_render_handler,
        },
    ]
