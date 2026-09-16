"""MCP tool definitions and handlers for Google Calendar.

Tool dicts are built via the declarative DSL's `CustomTool` wrapper (V4
chunk 4.1) rather than hand-assembled — each `handle_*` function below is
unchanged (they don't fit `ListTool`'s after/before-date shape: `days`-ahead
windows, OR-across-columns search, and visibility-filtering post-processing
are all bespoke), so `CustomTool` is the right DSL builder: it wraps an
existing handler without forcing it into a shape it doesn't have. Every tool
still declares its own inline `annotations` (unchanged values — pinned by
`tests/test_plugin_discovery.py`'s frozen `OLD_TOOL_ANNOTATIONS`).
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike

from app.integrations.google_calendar.models import CalendarEvent
from app.plugin.config_store import plugin_config
from app.tools import CustomTool, ToolAnnotations


def _apply_visibility(events: list[dict]) -> list[dict]:
    """Apply calendar visibility rules: full/busy/hidden.

    Keyed by the calendar the event lives on (e.g. a shared/subscribed
    family calendar), falling back to the authenticated account. Fixed
    2026-04-17 (bb7b4a9) after mum's and Sam's shared calendars leaked
    through despite being marked hidden, because the lookup was keyed only
    on `account` (always the authenticated Google account, never the
    subscribed calendar) — never regress that key order.

    ⚠️ This is a single, HOUSEHOLD-WIDE policy, not a per-viewer one — the
    config has no per-user dimension (`ConfigFieldSpec.type` has no nested
    shape to express "visible to the owner, busy for everyone else"), and
    `calendar_events` itself carries no `user_id` (the calendar is
    deliberately household-shared, see the `system` MCP instructions block).
    Every caller — whichever household member is asking — sees the same
    filtered set. If a future ask needs true per-viewer rules, the config
    shape has to grow a nested per-user layer; do not fake it by branching
    on `current_user_id()` here, since `plugin_config()` validates a stored
    `dict_str_str` value on every read and a shape change needs a deliberate
    migration (there is no Alembic-equivalent for config).

    ⚠️ This is the ONLY place visibility is applied. Every read path MUST
    route through `_serialize_events()` below (which calls this) rather than
    building its own dict from `CalendarEvent` rows and forgetting the
    filter — see that function's docstring.
    """
    visibility = plugin_config("google_calendar").calendar_visibility
    filtered = []
    for e in events:
        vis = visibility.get(e.get("calendar")) or visibility.get(e.get("account"), "full")
        if vis == "hidden":
            continue
        if vis == "busy":
            e = {**e, "summary": "(busy)", "description": None, "location": None}
        filtered.append(e)
    return filtered


def _serialize_events(events: list[CalendarEvent]) -> list[dict]:
    """The one conversion from `CalendarEvent` rows to the wire shape.

    Every read path (`query_events`, `handle_today`, the facade, the
    dashboard) must produce its list of events by calling this — never by
    hand-building a dict from ORM rows — so visibility filtering cannot be
    forgotten by a new consumer. See `_apply_visibility`'s docstring for why
    this is household-wide rather than per-viewer.
    """
    raw = [
        {
            "summary": e.summary,
            "calendar": e.calendar_name,
            "account": e.calendar_account,
            "start": e.start_time.isoformat(),
            "end": e.end_time.isoformat(),
            "all_day": e.all_day,
            "location": e.location,
            "description": e.description,
        }
        for e in events
    ]
    return _apply_visibility(raw)


def query_events(
    session: Session,
    days: int = 7,
    account: str | None = None,
    search: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Query cached calendar events from the DB with visibility filtering."""
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days)

    query = session.query(CalendarEvent).filter(
        CalendarEvent.start_time >= now,
        CalendarEvent.start_time <= time_max,
        CalendarEvent.status == "confirmed",
    )

    if account:
        query = query.filter(CalendarEvent.calendar_account == account)

    if search:
        # Split multi-word queries into OR conditions — any word matches
        terms = search.split()
        term_filters = []
        for term in terms:
            pattern = f"%{escape_ilike(term)}%"
            term_filters.append(
                or_(
                    CalendarEvent.summary.ilike(pattern, escape=ILIKE_ESCAPE_CHAR),
                    CalendarEvent.description.ilike(pattern, escape=ILIKE_ESCAPE_CHAR),
                )
            )
        if term_filters:
            query = query.filter(or_(*term_filters))

    query = query.order_by(CalendarEvent.start_time)

    # Bound even when no explicit limit was requested — `days` is caller-
    # controlled (handle_next_events passes 90) and an unbounded `.all()` on a
    # busy multi-account household calendar has no natural cap otherwise.
    # 500 events comfortably covers a month of a heavily-booked calendar
    # across 4+ accounts without truncating any realistic week/month view.
    query = query.limit(limit if limit else 500)

    events = query.all()

    return _serialize_events(events)


# --- MCP tool handlers ---
# Each handler receives (session, arguments) and returns a JSON-serializable result.


def handle_list_events(session: Session, arguments: dict[str, Any]) -> str:
    events = query_events(
        session,
        days=arguments.get("days", 7),
        account=arguments.get("account"),
        search=arguments.get("search"),
    )
    return json.dumps(events, indent=2)


def handle_today(session: Session, arguments: dict[str, Any]) -> str:
    # Query the full calendar day (midnight to midnight in Dublin time),
    # not "next 24h from now" — so morning events aren't excluded.
    tz = ZoneInfo("Europe/Dublin")
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    query = (
        session.query(CalendarEvent)
        .filter(
            CalendarEvent.start_time >= today_start,
            CalendarEvent.start_time < today_end,
            CalendarEvent.status == "confirmed",
        )
        .order_by(CalendarEvent.start_time)
        # A single calendar day, even across every family account, will never
        # realistically exceed this — bound it anyway so a data anomaly (e.g.
        # a mis-synced recurring event exploding) can't return unbounded rows.
        .limit(500)
    )
    events = query.all()

    return json.dumps(_serialize_events(events), indent=2)


def handle_next_events(session: Session, arguments: dict[str, Any]) -> str:
    count = arguments.get("count", 10)
    # Over-fetch to account for hidden events being filtered out
    events = query_events(session, days=90, limit=count * 3)
    return json.dumps(events[:count], indent=2)


def handle_create_event(session: Session, arguments: dict[str, Any]) -> str:
    """Create a calendar event on a Google Calendar account (scoped to the requesting user)."""
    from app.auth.context import current_user_id
    from app.integrations.google_calendar.client import create_event
    from app.models.tokens import OAuthToken

    summary = arguments.get("summary")
    if not summary:
        return json.dumps({"error": "summary is required"})

    start_time = arguments.get("start_time")
    if not start_time:
        return json.dumps({"error": "start_time is required"})

    uid = current_user_id()
    account = arguments.get("account")

    # Verify the requesting user owns the chosen account. If no account given,
    # pick that user's first Google token rather than hardcoding Alex's email.
    if account:
        owned = (
            session.query(OAuthToken)
            .filter_by(user_id=uid, provider="google", account_email=account)
            .first()
        )
        if not owned:
            return json.dumps({"error": f"account {account} not owned by current user"})
    else:
        owned = (
            session.query(OAuthToken)
            .filter_by(user_id=uid, provider="google")
            .first()
        )
        if not owned:
            return json.dumps({"error": "no Google account connected for current user"})
        account = owned.account_email

    # Attendees leave the house — a typo either bounces or invites a stranger to
    # a family appointment. Reject the whole call rather than silently dropping a
    # malformed address, which would report success having invited fewer people.
    attendees = arguments.get("attendees") or None
    if attendees is not None:
        if isinstance(attendees, str):
            attendees = [attendees]
        if not isinstance(attendees, list):
            return json.dumps({"error": "attendees must be a list of email addresses"})
        attendees = [str(a).strip() for a in attendees if str(a).strip()]
        bad = [a for a in attendees if a.count("@") != 1 or a.startswith("@") or a.endswith("@")]
        if bad:
            return json.dumps({"error": f"not valid email addresses: {bad}"})

    result = create_event(
        account_email=account,
        session=session,
        summary=summary,
        start_time=start_time,
        user_id=uid,
        end_time=arguments.get("end_time"),
        all_day=arguments.get("all_day", False),
        description=arguments.get("description"),
        location=arguments.get("location"),
        attendees=attendees,
        send_invites=arguments.get("send_invites", True),
    )

    if result is None:
        return json.dumps({"error": f"Failed to create event on {account}"})
    return json.dumps(result, indent=2)


def handle_update_event(session: Session, arguments: dict[str, Any]) -> str:
    """Update fields on an existing event (PATCH semantics — see
    `client.update_event`'s docstring for the full merge-vs-replace and
    recurring-event rationale). Mirrors `handle_create_event`'s account
    ownership check and attendee validation.
    """
    from app.auth.context import current_user_id
    from app.integrations.google_calendar.client import update_event
    from app.models.tokens import OAuthToken

    account = arguments.get("account")
    if not account:
        return json.dumps({"error": "account is required"})

    event_id = arguments.get("event_id")
    if not event_id:
        return json.dumps({"error": "event_id is required"})

    uid = current_user_id()

    # Decision A: never guess which account/calendar an event id belongs to
    # — verify the requesting user actually owns the named account first,
    # exactly as handle_create_event does.
    owned = (
        session.query(OAuthToken)
        .filter_by(user_id=uid, provider="google", account_email=account)
        .first()
    )
    if not owned:
        return json.dumps({"error": f"account {account} not owned by current user"})

    attendees = arguments.get("attendees") or None
    if attendees is not None:
        if isinstance(attendees, str):
            attendees = [attendees]
        if not isinstance(attendees, list):
            return json.dumps({"error": "attendees must be a list of email addresses"})
        attendees = [str(a).strip() for a in attendees if str(a).strip()]
        bad = [a for a in attendees if a.count("@") != 1 or a.startswith("@") or a.endswith("@")]
        if bad:
            return json.dumps({"error": f"not valid email addresses: {bad}"})

    # Any classified error (404 unknown event, 403 read-only calendar, etc.)
    # raises a ComarError here — deliberately NOT caught: `app.plugin.dispatch`
    # is the single chokepoint that turns a PermanentError/TransientError into
    # a structured tool-call error (and records it in the audit trail with
    # the right status), so re-catching it here would just duplicate that and
    # mis-record the call as a successful one.
    result = update_event(
        account_email=account,
        session=session,
        event_id=event_id,
        user_id=uid,
        calendar_id=arguments.get("calendar_id", "primary"),
        summary=arguments.get("summary"),
        start_time=arguments.get("start_time"),
        end_time=arguments.get("end_time"),
        all_day=arguments.get("all_day"),
        description=arguments.get("description"),
        location=arguments.get("location"),
        attendees=attendees,
        send_invites=arguments.get("send_invites", True),
    )

    if result is None:
        return json.dumps({"error": f"Failed to update event on {account}"})
    return json.dumps(result, indent=2)


def handle_delete_event(session: Session, arguments: dict[str, Any]) -> str:
    """Delete an event, returning captured detail (title/time/calendar) of
    what was removed — see `client.delete_event`'s docstring (decision D).
    """
    from app.auth.context import current_user_id
    from app.integrations.google_calendar.client import delete_event
    from app.models.tokens import OAuthToken

    account = arguments.get("account")
    if not account:
        return json.dumps({"error": "account is required"})

    event_id = arguments.get("event_id")
    if not event_id:
        return json.dumps({"error": "event_id is required"})

    uid = current_user_id()

    owned = (
        session.query(OAuthToken)
        .filter_by(user_id=uid, provider="google", account_email=account)
        .first()
    )
    if not owned:
        return json.dumps({"error": f"account {account} not owned by current user"})

    # See handle_update_event: classified errors deliberately propagate to
    # app.plugin.dispatch rather than being caught here.
    result = delete_event(
        account_email=account,
        session=session,
        event_id=event_id,
        user_id=uid,
        calendar_id=arguments.get("calendar_id", "primary"),
        send_updates=arguments.get("send_updates", True),
    )

    if result is None:
        return json.dumps({"error": f"Failed to delete event on {account}"})
    return json.dumps(result, indent=2)


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        CustomTool(
            name="calendar_list_events",
            description=(
                "List upcoming calendar events across all family calendars. "
                "Returns event title, time, location, and which calendar/account it's from. "
                "Defaults to the next 7 days. Optionally filter by account email or search text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days ahead to look (default 7)",
                        "default": 7,
                    },
                    "account": {
                        "type": "string",
                        "description": "Filter by account email (optional)",
                    },
                    "search": {
                        "type": "string",
                        "description": "Search text — multi-word queries match ANY word in event title or description (optional)",
                    },
                },
            },
            handler=handle_list_events,
            annotations=ToolAnnotations(
                read_only_hint=True, idempotent_hint=True, open_world_hint=True,
            ),
            category="calendar",
            examples=[
                "What's on this week?",
                "Any events next Thursday?",
                "Search for dentist appointments",
            ],
        ).build(),
        CustomTool(
            name="calendar_today",
            description=(
                "Show today's calendar events with times, locations, and which calendar "
                "they're from. Work events show as '(busy)' for privacy. "
                "Use this to check what's on today."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_today,
            annotations=ToolAnnotations(
                read_only_hint=True, idempotent_hint=True, open_world_hint=True,
            ),
            category="calendar",
            examples=[
                "What's on today?",
                "Do I have any meetings today?",
            ],
        ).build(),
        CustomTool(
            name="calendar_next_events",
            description=(
                "Get the next N upcoming events across all calendars, regardless of date. "
                "Useful for a quick glance at what's coming up soon."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of events to return (default 10)",
                        "default": 10,
                    },
                },
            },
            handler=handle_next_events,
            annotations=ToolAnnotations(
                read_only_hint=True, idempotent_hint=True, open_world_hint=True,
            ),
            category="calendar",
            examples=[
                "What's coming up next?",
                "Show me the next 5 events",
            ],
        ).build(),
        CustomTool(
            name="calendar_create_event",
            description=(
                "Create a new event on a Google Calendar. Use for scheduling appointments, "
                "reminders with calendar entries, or blocking time. Defaults to the "
                "calling user's first connected Google account."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Event title",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start time as ISO 8601 datetime (e.g. '2026-04-15T10:00:00') or YYYY-MM-DD for all-day events",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time (optional — defaults to start + 1 hour for timed, + 1 day for all-day)",
                    },
                    "all_day": {
                        "type": "boolean",
                        "description": "Whether this is an all-day event (default false)",
                        "default": False,
                    },
                    "description": {
                        "type": "string",
                        "description": "Event description/notes (optional)",
                    },
                    "location": {
                        "type": "string",
                        "description": "Event location (optional)",
                    },
                    "account": {
                        "type": "string",
                        "description": "Google account email to create event on (default: the caller's first connected account)",
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Email addresses to invite. Each attendee is emailed a "
                            "calendar invitation unless send_invites is false."
                        ),
                    },
                    "send_invites": {
                        "type": "boolean",
                        "description": (
                            "Email the attendees an invitation (default true). Set false "
                            "to attach people without notifying them — e.g. recording who "
                            "was at something after the fact."
                        ),
                        "default": True,
                    },
                },
                "required": ["summary", "start_time"],
            },
            handler=handle_create_event,
            annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True),
            category="calendar",
            examples=[
                "Create a calendar event for the dentist on April 15th at 2pm",
                "Block time for house viewing on the 20th",
                "Add an all-day reminder for a birthday",
            ],
        ).build(),
        CustomTool(
            name="calendar_update_event",
            description=(
                "Update fields on an existing Google Calendar event — only the fields you "
                "pass are changed, everything else (attendees, description, location, "
                "recurrence) is left exactly as it was. You must specify the account the "
                "event lives on and its event id (from calendar_list_events/calendar_today) "
                "— this tool never guesses which account or calendar an event id belongs "
                "to. Not supported: events that are part of a recurring series (they have "
                "a 'recurrence' or 'recurringEventId' field) — edit those directly in "
                "Google Calendar."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Google account email the event lives on (required — e.g. 'user@example.com')",
                    },
                    "event_id": {
                        "type": "string",
                        "description": "The event's Google event id, as returned by calendar_list_events/calendar_today",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar id the event lives on (default 'primary' — events created by calendar_create_event are always on 'primary')",
                        "default": "primary",
                    },
                    "summary": {
                        "type": "string",
                        "description": "New event title (optional — omit to leave unchanged)",
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "New start time as ISO 8601 datetime, or YYYY-MM-DD for "
                            "all-day events (optional — omit to leave unchanged). If "
                            "given without end_time, only the start moves; pass both "
                            "to shift the whole event."
                        ),
                    },
                    "end_time": {
                        "type": "string",
                        "description": "New end time (optional — omit to leave unchanged)",
                    },
                    "all_day": {
                        "type": "boolean",
                        "description": (
                            "Whether the event should be all-day (optional — only needed "
                            "when switching between timed and all-day; otherwise inferred "
                            "from start_time)"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "New event description/notes (optional — omit to leave unchanged)",
                    },
                    "location": {
                        "type": "string",
                        "description": "New event location (optional — omit to leave unchanged)",
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Full replacement list of attendee email addresses (optional — "
                            "omit to leave the current attendee list unchanged)"
                        ),
                    },
                    "send_invites": {
                        "type": "boolean",
                        "description": "Email attendees about this change (default true)",
                        "default": True,
                    },
                },
                "required": ["account", "event_id"],
            },
            handler=handle_update_event,
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
            ),
            category="calendar",
            examples=[
                "Move the dentist appointment to 3pm instead of 2pm",
                "Fix the typo in the title of tomorrow's meeting",
                "Add a location to the house viewing event",
            ],
        ).build(),
        CustomTool(
            name="calendar_delete_event",
            description=(
                "Permanently delete a Google Calendar event. Returns the title, time, and "
                "calendar of what was removed so the caller can confirm what happened — "
                "Google's own trash/recovery window is limited, so this is not fully "
                "reversible after that window passes. You must specify the account the "
                "event lives on and its event id (from calendar_list_events/calendar_today) "
                "— this tool never guesses which account or calendar an event id belongs "
                "to, and never deletes an event you haven't identified precisely. An "
                "unknown event id raises a clear error rather than doing nothing silently. "
                "Not supported: events that are part of a recurring series (they have a "
                "'recurrence' or 'recurringEventId' field) — cancel those directly in "
                "Google Calendar."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Google account email the event lives on (required — e.g. 'user@example.com')",
                    },
                    "event_id": {
                        "type": "string",
                        "description": "The event's Google event id, as returned by calendar_list_events/calendar_today",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar id the event lives on (default 'primary' — events created by calendar_create_event are always on 'primary')",
                        "default": "primary",
                    },
                    "send_updates": {
                        "type": "boolean",
                        "description": "Email existing attendees that the event was cancelled (default true)",
                        "default": True,
                    },
                },
                "required": ["account", "event_id"],
            },
            handler=handle_delete_event,
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
            ),
            category="calendar",
            examples=[
                "Cancel the dentist appointment",
                "Delete the house viewing event, it fell through",
            ],
        ).build(),
    ]
