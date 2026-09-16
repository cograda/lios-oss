"""MCP tools for the snag register. The DB is the source of truth; every
write re-renders the vault view (Household/Renovation/Snags.md).

  - snag_capture: parse 'Snag - …' WhatsApp messages into snags (idempotent)
  - snag_list:    filter by status/trade/room/severity/text
  - snag_add:     manual entry (walkthroughs, phone calls)
  - snag_update:  triage by UID — status, trade, severity, notes, external ref
  - snag_render:  force a re-render of the vault note
  - snag_export_pdf: render the register as a PDF for the builder/engineer
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.snags.capture import capture_whatsapp_snags, _next_uid
from app.integrations.snags.models import (
    SEVERITIES, STATUSES, Snag, SnagMedia,
)
from app.integrations.snags.pdf import render_snags_pdf
from app.integrations.snags.render import OPEN_STATUSES, render_snags_note
from app.integrations.snags.vocab import allowed_trades
from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import iso_or_none, serialize

logger = logging.getLogger(__name__)

_ROW_FIELDS = [
    "uid", "title", "room", "element", "trade", "severity", "status",
    "reported_by", "reported_at", "external_ref", "resolution_note",
]


def _row(s: Snag, media_count: int | None = None) -> dict:
    out = serialize(s, _ROW_FIELDS, transforms={"reported_at": iso_or_none})
    out["media_count"] = media_count
    return out


def _render(session: Session) -> str:
    path = render_snags_note(session)
    _export_to_sheet(session)
    return path


def _export_to_sheet(session: Session) -> None:
    """Best-effort mirror of the snag register into a shared Google Sheet.

    Never raises — a Sheets/Drive hiccup must not block a snag write (the
    vault render above already succeeded and is the primary view).

    **Whose Google token (2026-09-06):** the CALLER's. Every path into here is
    a tool call (`snag_add`/`snag_update`/`snag_capture`/`snag_render` —
    snags has no schedule and no background task), so there is always a
    bound user, and the same decision that retired `docs_owner_account` for
    Google Docs in PR #122 applies: no tool may act with another household
    member's Google token. The configured `sheets_owner_account` is gone.
    Consequences, stated plainly in the log rather than hidden:

    - A caller with no Google account connected: export skipped, with the
      same message the Docs tools give.
    - The sheet was created by someone else (the `sheet_exports` row records
      its owner): export skipped — that sheet is theirs to mirror. Nothing
      here will ever fetch their token to do it for them.
    """
    from app.auth.context import current_user_id_or_none

    caller_id = current_user_id_or_none()
    if caller_id is None:
        logger.info("[snags] no bound user, skipping sheet export")
        return
    try:
        from app.models.tokens import OAuthToken
        from app.plugin.capabilities import get_capability
        from app.plugin.config_store import plugin_config

        snags_cfg = plugin_config("snags")
        sheets = get_capability("sheets.write")

        token = (
            session.query(OAuthToken)
            .filter_by(provider="google", user_id=caller_id)
            .first()
        )
        if token is None:
            logger.warning(
                "[snags] user %s has no Google account connected, skipping sheet "
                "export — connect your own account from the dashboard (Settings → "
                "Connect; the /api/auth/google/login link needs a signed start the "
                "dashboard mints). Tools never use "
                "another household member's Google token (decision of 2026-09-06).",
                caller_id,
            )
            return

        existing = session.query(sheets.Export).filter_by(key="snags").one_or_none()
        if existing is not None and existing.owner_account_email != token.account_email:
            logger.warning(
                "[snags] the snag sheet is owned by %s; user %s's account %s cannot "
                "mirror it, skipping sheet export",
                existing.owner_account_email, caller_id, token.account_email,
            )
            return

        export = sheets.ensure_export(
            session,
            key="snags",
            title="Comar — Snag Register",
            owner_account_email=token.account_email,
            owner_user_id=caller_id,
            share_with=snags_cfg.sheets_share_with,
        )
        if export is None:
            return

        headers = [
            "UID", "Title", "Room", "Element", "Trade", "Severity", "Status",
            "Reported By", "Reported At", "External Ref", "Resolution Note",
        ]
        rows = [
            [
                s.uid, s.title, s.room, s.element or "", s.trade, s.severity, s.status,
                s.reported_by or "",
                s.reported_at.strftime("%Y-%m-%d") if s.reported_at else "",
                s.external_ref or "", s.resolution_note or "",
            ]
            for s in session.query(Snag).order_by(Snag.trade, Snag.room, Snag.id).all()
        ]
        sheets.write_rows(session, export, owner_user_id=caller_id, headers=headers, rows=rows)
    except Exception:
        logger.exception("[snags] sheet export failed, continuing")


def _sheet_url(session: Session) -> str | None:
    from app.plugin.capabilities import get_capability

    sheets = get_capability("sheets.write")
    export = session.query(sheets.Export).filter_by(key="snags").one_or_none()
    return export.spreadsheet_url if export else None


def snag_capture_handler(session: Session, arguments: dict) -> str:
    since_days = int(arguments.get("since_days") or 7)
    result = capture_whatsapp_snags(session, since_days=since_days)
    if result["snags_created"]:
        result["rendered"] = _render(session)
        result["sheet_url"] = _sheet_url(session)
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
        q = q.filter(
            Snag.room.ilike(f"%{escape_ilike(arguments['room'])}%", escape=ILIKE_ESCAPE_CHAR)
        )
    if arguments.get("severity"):
        q = q.filter(Snag.severity == arguments["severity"])
    if arguments.get("query"):
        pat = f"%{escape_ilike(arguments['query'])}%"
        q = q.filter(
            Snag.title.ilike(pat, escape=ILIKE_ESCAPE_CHAR)
            | Snag.description.ilike(pat, escape=ILIKE_ESCAPE_CHAR)
        )
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

    # Validate here rather than via a JSON-schema enum: the allowed trades are
    # deployment config unioned with what's already in the table, and both
    # need a DB session — which tool-schema construction must not require.
    for field, allowed in (
        ("trade", allowed_trades(session)), ("severity", SEVERITIES),
    ):
        val = arguments.get(field)
        if val and val not in allowed:
            return json.dumps({
                "status": "error",
                "detail": f"{field} must be one of {list(allowed)}",
            })

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

    from app.plugin.dispatch import set_affected
    set_affected([f"snag:{snag.uid}"])

    return json.dumps({"created": _row(snag), "rendered": rendered, "sheet_url": _sheet_url(session)})


def snag_update_handler(session: Session, arguments: dict) -> str:
    uid = (arguments.get("uid") or "").strip().upper()
    snag = session.query(Snag).filter(Snag.uid == uid).one_or_none()
    if not snag:
        return json.dumps({"status": "error", "detail": f"no snag with uid {uid!r}"})

    changed = {}
    for field, allowed in (
        ("status", STATUSES), ("trade", allowed_trades(session)), ("severity", SEVERITIES),
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
        from app.plugin.capabilities import get_capability

        MediaItem = get_capability("media.store").Item

        attached = []
        for mid in arguments["attach_media_ids"]:
            # F5: MediaItem is UserOwnedMixin — the snag register itself is
            # household-shared BY DESIGN, but that must not make it a channel
            # for one user's WhatsApp media to reach the other implicitly.
            # The check lives here, at attach time (a deliberate act by
            # whichever user is calling snag_update), not at render time —
            # once attached, the evidence is meant to be visible to both.
            item = session.get(MediaItem, int(mid))
            if item is None or item.user_id != current_user_id():
                return json.dumps({
                    "status": "error",
                    "detail": f"no media item with id {mid} owned by the current user",
                })
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

    from app.plugin.dispatch import set_affected
    set_affected([f"snag:{snag.uid}"])

    return json.dumps({
        "updated": _row(snag), "changed": changed, "rendered": rendered,
        "sheet_url": _sheet_url(session),
    })


def snag_render_handler(session: Session, arguments: dict) -> str:
    rendered = _render(session)
    return json.dumps({"rendered": rendered, "sheet_url": _sheet_url(session)})


def snag_export_pdf_handler(session: Session, arguments: dict) -> str:
    path = render_snags_pdf(session)
    return json.dumps({"rendered": path})


def mcp_tools() -> list[dict[str, Any]]:
    return [
        CustomTool(
            name="snag_capture",
            description=(
                "Scan WhatsApp for structured snag reports ('Snag - room - trade "
                "- detail' texts or photo captions) and register them in the snag "
                "database with UIDs, linking evidence photos from the media store. "
                "Idempotent — already-captured messages are skipped. Re-renders "
                "the vault Snags note and the shared Google Sheet if configured "
                "(returns sheet_url when snags_created > 0)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since_days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
                },
            },
            handler=snag_capture_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="snag_list",
            description=(
                "List snags from the register (open ones by default). Filter by "
                "status, trade, room, severity, or free-text. Each snag has a "
                "stable UID (SNAG-0042) for tracking with trades."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "include_closed": {"type": "boolean", "default": False},
                    "trade": {
                        "type": "string",
                        "description": (
                            "Trade/contractor slug. Allowed values are "
                            "deployment config (snags.trades) rather than a "
                            "fixed enum, so they are validated on write and "
                            "the error lists them."
                        ),
                    },
                    "room": {"type": "string"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "query": {"type": "string", "description": "Substring in title/description."},
                    "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                },
            },
            handler=snag_list_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="snag_add",
            description=(
                "Manually add a snag (walkthrough findings, phone reports). "
                "Assigns the next UID, re-renders the vault note, and mirrors "
                "into the shared Google Sheet if configured (returns sheet_url)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "room": {"type": "string"},
                    "description": {"type": "string"},
                    "element": {"type": "string"},
                    "trade": {
                        "type": "string",
                        "description": (
                            "Trade/contractor slug. Allowed values are "
                            "deployment config (snags.trades) rather than a "
                            "fixed enum, so they are validated on write and "
                            "the error lists them."
                        ),
                    },
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "reported_by": {"type": "string"},
                },
                "required": ["title", "room"],
            },
            handler=snag_add_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="snag_update",
            description=(
                "Triage/update a snag by UID: reassign trade, set severity, move "
                "status through the lifecycle (open → reported → accepted/disputed "
                "→ fixed → verified → closed, or wont-fix), attach the trade's "
                "ticket number, or record a resolution note. Sets "
                "reported_to_trade_at / resolved_at automatically. Re-renders "
                "the vault note and the shared Google Sheet if configured."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "e.g. SNAG-0042"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "trade": {
                        "type": "string",
                        "description": (
                            "Trade/contractor slug. Allowed values are "
                            "deployment config (snags.trades) rather than a "
                            "fixed enum, so they are validated on write and "
                            "the error lists them."
                        ),
                    },
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
            handler=snag_update_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="snag_render",
            description=(
                "Force a re-render of the generated vault note "
                "(Household/Renovation/Snags.md) from the snag database, "
                "exporting any missing evidence photos to Attachments/Snags/. "
                "Also re-syncs the shared Google Sheet if configured (returns sheet_url)."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=snag_render_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="snag_export_pdf",
            description=(
                "Export the snag register as a PDF, for handing to the builder "
                "or engineer. Same snags, grouping (trade then room) and status "
                "lifecycle as the vault note and shared Sheet, plus a status "
                "legend and evidence photos where already exported. Written "
                "into the vault at Household/Renovation/Snags.pdf, next to the "
                "generated markdown note; open it there (e.g. via Obsidian)."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=snag_export_pdf_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
    ]
