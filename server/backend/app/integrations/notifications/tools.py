"""MCP tools for notifications.

Only two, both about the sweep rather than replacing it: send an ad-hoc push
(also the way to verify config end-to-end without waiting for something to
break), and read the ledger to answer "did I actually get told about this?".

Sending is not read-only but it *is* harmless and repeatable — `open_world_hint`
is what flags that it leaves the server, and `destructive_hint=False` says a
duplicate push costs nothing but a buzz.

**2026-09-04 — `notify_recent` can actually answer its own question now.**
Before this, `handle_send` called `client.publish()` with no `source` and
`publish()` wrote no ledger row at all for a non-sweep call — measured on
`vault/Projects/lios/Backlog.md`'s "Ad-hoc pushes are never ledgered" item:
an ad-hoc send left `notification_sends` at 233 rows before *and* after.
`publish()` now ledgers every call by default (`client.py`'s "Ledger every
publish" note); this file's only change is tagging its one caller
`source="tool"` and surfacing `source`/`send_status`/`error` here.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.auth.context import current_user_id, current_user_id_or_none
from app.errors import ComarError
from app.integrations.notifications import client
from app.integrations.notifications.models import NotificationSend
from app.tools import CustomTool, ToolAnnotations


def handle_send(session: Session, arguments: dict[str, Any]) -> str:
    """Publish one ad-hoc message via the configured Home Assistant push sink."""
    message = (arguments.get("message") or "").strip()
    if not message:
        return json.dumps({"error": "message is required"})

    title = (arguments.get("title") or "lios").strip()
    severity = arguments.get("severity") or "warning"
    if severity not in ("critical", "warning", "recovery"):
        return json.dumps(
            {"error": "severity must be one of: critical, warning, recovery"}
        )

    # 2026-09-06 — one credential: the per-user bearer. The tool used to take
    # `user_id` from its *arguments*, so any caller could buzz any user's phone
    # with no check that the caller was that user. The recipient is now the
    # bound caller, full stop; `household=true` opts into `publish()`'s
    # existing `user_id=None` fan-out to every `household_targets` entry. A
    # push to *another* person is a server-side decision (tasks nudge /
    # transfer, absence alerts, capture confirmations — all of which call
    # `client.publish(..., user_id=...)` directly), never a tool argument.
    household = bool(arguments.get("household", False))
    user_id: int | None = None if household else current_user_id()

    try:
        # `source="tool"` — this is the ad-hoc `notify_send` path the
        # "Ad-hoc pushes are never ledgered" backlog item names directly.
        # `publish()` now writes a `notification_sends` row itself
        # (ledger=True is the default) whether this succeeds or raises, so
        # a failed ad-hoc send is a `status="failed"` row rather than
        # silence — see `client.py::_record_send`.
        client.publish(title, message, severity, user_id=user_id, source="tool")
    except ComarError as exc:
        # Surfaced as data, not an exception: the config error names the exact
        # keys to set, which is more useful to the caller than a stack trace.
        return json.dumps({"sent": False, "error": str(exc)})

    return json.dumps({"sent": True, "title": title, "severity": severity})


def handle_recent(session: Session, arguments: dict[str, Any]) -> str:
    """List recent ledger rows, newest first.

    Includes ad-hoc sends (`source` != "sweep") since 2026-09-04 — see the
    module docstring history and `client.py::_record_send`. `source` filters
    to one caller kind when given.
    """
    limit = min(int(arguments.get("limit", 20)), 100)
    open_only = bool(arguments.get("open_only", False))
    source = arguments.get("source")

    query = session.query(NotificationSend)
    if open_only:
        query = query.filter(NotificationSend.resolved_at.is_(None))
    if source:
        query = query.filter(NotificationSend.source == source)

    # F7: most rows are household-shared (user_id NULL — a stale-sync alert
    # has no single owner), but some (the health data-coverage gap) name a
    # specific user, and that body text is real per-user data. A bound caller
    # sees the shared rows plus their own; unbound sees shared rows only —
    # per `auth/context.py`'s documented stance that unbound means
    # "household-shared," never "everyone's private data."
    caller_uid = current_user_id_or_none()
    if caller_uid is None:
        query = query.filter(NotificationSend.user_id.is_(None))
    else:
        query = query.filter(
            (NotificationSend.user_id.is_(None))
            | (NotificationSend.user_id == caller_uid)
        )

    rows = query.order_by(NotificationSend.first_seen_at.desc()).limit(limit).all()

    return json.dumps(
        {
            "count": len(rows),
            "notifications": [
                {
                    "fingerprint": row.fingerprint,
                    "title": row.title,
                    "body": row.body,
                    "severity": row.severity,
                    "status": "open" if row.resolved_at is None else "resolved",
                    "source": row.source,
                    "send_status": row.status,
                    "error": row.error_text,
                    "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
                    "last_sent_at": row.last_sent_at.isoformat() if row.last_sent_at else None,
                    "send_count": row.send_count,
                    "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                    # Push-boundary gating (2026-08-27): non-null while a
                    # detected, still-open alert is being deliberately held
                    # back from a phone (persistence gate / re-fire cooldown
                    # / quiet hours) rather than actually delivered.
                    "suppressed_reason": row.suppressed_reason,
                }
                for row in rows
            ],
        },
        indent=2,
    )


def get_mcp_tools() -> list[dict]:
    return [
        CustomTool(
            name="notify_send",
            description=(
                "Send a push notification via the household's configured Home "
                "Assistant mobile-app targets (reaches subscribed phones). "
                "Use for something the user asked to be told about away from "
                "the terminal, or to verify notification config is working. "
                "Reaches the CALLER's own device (their entry in the "
                "notifications integration's `targets` config); pass "
                "household=true to fan out to the household-wide "
                "`household_targets` instead. There is no way to name another "
                "person — pushes to someone else are server-side decisions "
                "(task nudges, absence alerts), not a tool argument."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Body of the notification.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Notification title (default: 'lios').",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "warning", "recovery"],
                        "description": (
                            "Maps to a Home Assistant push priority/interruption "
                            "level. 'critical' can bypass Do Not Disturb (on iOS, "
                            "only if the recipient has granted Critical Alerts "
                            "permission for this app) — reserve it for something "
                            "needing action now."
                        ),
                    },
                    "household": {
                        "type": "boolean",
                        "description": (
                            "Fan out to the household-wide `household_targets` "
                            "instead of only the caller's own device (default "
                            "false)."
                        ),
                        "default": False,
                    },
                },
                "required": ["message"],
            },
            handler=handle_send,
            annotations=ToolAnnotations(
                title="Send push notification",
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,  # each call is another buzz
                open_world_hint=True,   # leaves the server for Home Assistant
            ),
        ).build(),
        CustomTool(
            name="notify_recent",
            description=(
                "List recent alert notifications from the send ledger — what "
                "was pushed, when, how often, and whether it has since "
                "resolved. Use to check whether an outage was actually "
                "announced, or which alerts are currently open."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max rows to return (default 20, max 100).",
                        "default": 20,
                    },
                    "open_only": {
                        "type": "boolean",
                        "description": "Only unresolved alerts (default false).",
                        "default": False,
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Filter to one caller kind, e.g. 'sweep' (the "
                            "automated alert sweep) or 'tool' (notify_send). "
                            "Omit for every source."
                        ),
                    },
                },
            },
            handler=handle_recent,
            annotations=ToolAnnotations(
                title="Recent notifications",
                read_only_hint=True,
                idempotent_hint=True,
            ),
        ).build(),
    ]
