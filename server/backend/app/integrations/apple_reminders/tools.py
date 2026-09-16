"""MCP tool definitions and handlers for Apple Reminders."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.apple_reminders.commands import dispatch_command
from app.integrations.apple_reminders.models import Reminder
from app.models.users import User
from app.tools import CustomTool, ToolAnnotations

logger = logging.getLogger(__name__)

# Priority mapping: Apple → human-readable
PRIORITY_MAP = {0: "none", 1: "high", 5: "medium", 9: "low"}
PRIORITY_REVERSE = {"high": 1, "medium": 5, "low": 9, "none": 0}


def _reminder_to_dict(r: Reminder) -> dict:
    return {
        "uid": r.uid,
        "list": r.list_name,
        "summary": r.summary,
        "notes": r.notes,
        "due_date": r.due_date.isoformat() if r.due_date else None,
        "priority": PRIORITY_MAP.get(r.priority, "none"),
        "completed": r.completed,
    }


# --- MCP tool handlers ---


def handle_list_reminders(session: Session, arguments: dict[str, Any]) -> str:
    """List incomplete reminders, optionally filtered by list."""
    uid = current_user_id()
    list_name = arguments.get("list")
    query = (
        session.query(Reminder)
        .filter(Reminder.user_id == uid, Reminder.completed == False)  # noqa: E712
    )

    if list_name:
        query = query.filter(Reminder.list_name == list_name)

    query = query.order_by(Reminder.priority.asc(), Reminder.due_date.asc().nullslast())
    reminders = query.all()

    return json.dumps([_reminder_to_dict(r) for r in reminders], indent=2)


def handle_sync_reminders(session: Session, arguments: dict[str, Any]) -> str:
    """Return open reminders plus what changed since a given timestamp.

    Single-call replacement for ``reminders_list`` that also surfaces items the
    user completed, added, or edited on their phone since ``since``. Primary
    use case: ``/daily-note`` needs to know what was ticked off since the last
    daily note so those items don't silently disappear from the briefing.

    Arguments:
        since: ISO 8601 timestamp. Defaults to 24 hours ago if omitted.

    Returns a JSON payload with:
        since:               echo of the effective `since` timestamp
        synced_at:           MAX(Reminder.synced_at) — most recent data-change
                             time across the user's reminders. Stays put when
                             nothing has changed; **do not use as a liveness
                             signal**.
        bridge_verified_at:  last time the comar-client reminders loop pinged
                             the server (every ~30s when healthy). **This is
                             the liveness signal** — if it's more than a few
                             minutes stale the EventKit bridge is offline.
        open:                all currently-incomplete reminders
        completed_since:     reminders marked done since `since` (most recent first)
        added_since:         reminders created since `since`
        edited_since:        open reminders whose summary/priority/due changed
                             since `since` (pre-existing, not new)
    """
    since_arg = arguments.get("since")
    if since_arg:
        try:
            since = datetime.fromisoformat(str(since_arg).replace("Z", "+00:00"))
        except ValueError:
            return json.dumps({"error": f"Invalid 'since' timestamp: {since_arg}"})
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=24)

    uid = current_user_id()
    open_reminders = (
        session.query(Reminder)
        .filter(Reminder.user_id == uid, Reminder.completed == False)  # noqa: E712
        .order_by(Reminder.priority.asc(), Reminder.due_date.asc().nullslast())
        .all()
    )

    completed_since = (
        session.query(Reminder)
        .filter(
            Reminder.user_id == uid,
            Reminder.completed == True,  # noqa: E712
            Reminder.completed_date.isnot(None),
            Reminder.completed_date > since,
        )
        .order_by(Reminder.completed_date.desc())
        .all()
    )

    added_since = (
        session.query(Reminder)
        .filter(Reminder.user_id == uid, Reminder.created_at > since)
        .order_by(Reminder.created_at.desc())
        .all()
    )

    # Pre-existing open items whose fields changed since `since`.
    # We use synced_at (touched on every push) combined with created_at
    # to exclude brand-new items (already in `added_since`).
    edited_since = (
        session.query(Reminder)
        .filter(
            Reminder.user_id == uid,
            Reminder.completed == False,  # noqa: E712
            Reminder.created_at <= since,
            Reminder.synced_at > since,
        )
        .order_by(Reminder.synced_at.desc())
        .all()
    )

    max_synced = (
        session.query(sqlfunc.max(Reminder.synced_at))
        .filter(Reminder.user_id == uid)
        .scalar()
    )

    # Bridge liveness — last time the daemon pinged us. Distinct from
    # max(Reminder.synced_at), which only moves when data actually changes.
    from app.models.users import User as _User
    user_row = session.query(_User).filter_by(id=uid).first()
    bridge_verified_at = user_row.reminders_verified_at if user_row else None

    return json.dumps(
        {
            "since": since.isoformat(),
            "synced_at": max_synced.isoformat() if max_synced else None,
            "bridge_verified_at": (
                bridge_verified_at.isoformat() if bridge_verified_at else None
            ),
            "open": [_reminder_to_dict(r) for r in open_reminders],
            "completed_since": [_reminder_to_dict(r) for r in completed_since],
            "added_since": [_reminder_to_dict(r) for r in added_since],
            "edited_since": [_reminder_to_dict(r) for r in edited_since],
        },
        indent=2,
    )


def handle_lists(session: Session, arguments: dict[str, Any]) -> str:
    """List all reminder lists with counts."""
    from sqlalchemy import func as sqlfunc
    uid = current_user_id()
    results = (
        session.query(
            Reminder.list_name,
            sqlfunc.count(Reminder.id),
        )
        .filter(Reminder.user_id == uid, Reminder.completed == False)  # noqa: E712
        .group_by(Reminder.list_name)
        .all()
    )
    lists = [{"name": name, "count": count} for name, count in results]
    return json.dumps(lists, indent=2)


def _resolve_user_name(session: Session, user_id: int) -> str | None:
    user = session.get(User, user_id)
    return user.name if user else None


def handle_add_reminder(session: Session, arguments: dict[str, Any]) -> str:
    """Server-side reminders_add: queue an EventKit command and dispatch via SSE.

    The connected daemon for this user executes EventKit locally, then POSTs
    /api/v1/reminders/commands/{id}/done. We wait briefly for the ack so the
    happy path returns synchronously; otherwise we return queued=True.
    """
    summary = (arguments.get("summary") or "").strip()
    if not summary:
        return json.dumps({"error": "summary is required"})

    user_id = current_user_id()
    user_name = _resolve_user_name(session, user_id)
    if not user_name:
        return json.dumps({"error": f"user {user_id} not found"})

    args = {
        "summary": summary,
        "list": arguments.get("list", "Reminders"),
        "due_date": arguments.get("due_date"),
        "priority": arguments.get("priority", "none"),
        "notes": arguments.get("notes"),
        "account_email": arguments.get("account_email"),
    }
    result = dispatch_command(
        session, user_id=user_id, user_name=user_name, action="add", args=args,
    )

    from app.plugin.dispatch import set_affected
    cmd_id = result.get("command_id")
    if cmd_id is not None:
        set_affected([f"reminder:cmd-{cmd_id}"])

    return json.dumps(result)


def handle_complete_reminder(session: Session, arguments: dict[str, Any]) -> str:
    """Server-side reminders_complete: queue + dispatch via SSE."""
    uid = (arguments.get("uid") or "").strip()
    if not uid:
        return json.dumps({"error": "uid is required"})

    user_id = current_user_id()
    user_name = _resolve_user_name(session, user_id)
    if not user_name:
        return json.dumps({"error": f"user {user_id} not found"})

    result = dispatch_command(
        session, user_id=user_id, user_name=user_name,
        action="complete", args={"uid": uid},
    )

    from app.plugin.dispatch import set_affected
    set_affected([f"reminder:{uid}"])

    return json.dumps(result)


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions.

    reminders_add / reminders_complete were client-side EventKit tools from
    2026-03-30 onward; in V3 D.5 (2026-05-02) they returned to the server as
    SSE-dispatched commands, so a single MCP server can serve both Macs.

    All six built via `CustomTool` (V4 chunk 4.3, batch C) — none of these
    handlers fit `ListTool`/`SearchTool`/`StatsTool`'s shape, so this is a
    pure dict-literal -> DSL-builder conversion with identical output.
    """
    return [
        CustomTool(
            name="reminders_list",
            description=(
                "List incomplete Apple Reminders with titles, due dates, priorities, "
                "and which list they belong to. Sorted by priority (urgent first) then "
                "due date. Use this to check what tasks are outstanding."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "list": {
                        "type": "string",
                        "description": "Filter to a specific reminder list (e.g. 'Reminders', 'Shopping'). Omit for all lists.",
                    },
                },
            },
            handler=handle_list_reminders,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="tasks",
            examples=[
                "What reminders do I have?",
                "Show my shopping list",
                "What's overdue?",
            ],
        ).build(),

        CustomTool(
            name="reminders_sync",
            description=(
                "Return open reminders plus what changed since a given timestamp "
                "(completed, added, edited). Use this instead of reminders_list "
                "at the start of a daily note or any multi-day catch-up — it "
                "surfaces items ticked off on the phone since the last check so "
                "they don't silently disappear. Two timestamp fields: "
                "`synced_at` is data-change time (only moves when reminders "
                "actually change — do NOT treat as liveness); "
                "`bridge_verified_at` is the daemon liveness signal (the "
                "comar-client pings it every ~30s) — warn if more than a few "
                "minutes stale, that means the EventKit bridge is offline."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": (
                            "ISO 8601 timestamp. Only changes after this moment "
                            "are returned. Defaults to 24 hours ago."
                        ),
                    },
                },
            },
            handler=handle_sync_reminders,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            category="tasks",
            examples=[
                "What reminders were completed since yesterday?",
                "Show open reminders plus recent changes",
                "Sync reminders and catch me up",
            ],
        ).build(),

        CustomTool(
            name="reminders_lists",
            description=(
                "List all Apple Reminder lists with the count of incomplete items in each. "
                "Use this to see which lists have items that need attention."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_lists,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="tasks",
            examples=[
                "What reminder lists do I have?",
                "How many reminders in each list?",
            ],
        ).build(),

        CustomTool(
            name="reminders_add",
            description=(
                "Create a new Apple Reminder via the user's connected daemon "
                "(EventKit). Appears on all the user's Apple devices within "
                "seconds via iCloud. Returns synced=true if the daemon "
                "acknowledged in time, queued=true otherwise (it will run "
                "when the daemon reconnects)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "The reminder text (required).",
                    },
                    "list": {
                        "type": "string",
                        "description": "Reminder list to add to (default: 'Reminders').",
                        "default": "Reminders",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes (optional).",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date in ISO format (optional).",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["none", "low", "medium", "high"],
                        "default": "none",
                    },
                    "account_email": {
                        "type": "string",
                        "description": (
                            "Optional. iCloud account email when the user has "
                            "more than one signed in on the executing Mac."
                        ),
                    },
                },
                "required": ["summary"],
            },
            handler=handle_add_reminder,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            category="tasks",
            examples=[
                "Remind me to pick up the kids at 3pm",
                "Add bread to the shopping list",
            ],
        ).build(),

        CustomTool(
            name="reminders_complete",
            description=(
                "Mark a reminder as completed via the user's connected daemon "
                "(EventKit). Use the UID from reminders_list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "The unique ID of the reminder to complete.",
                    },
                },
                "required": ["uid"],
            },
            handler=handle_complete_reminder,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            category="tasks",
            examples=[
                "Mark that reminder done",
            ],
        ).build(),
    ]
