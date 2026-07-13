"""MCP tool definitions and handlers for Google Calendar."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.google_calendar.models import CalendarEvent


def _apply_visibility(events: list[dict]) -> list[dict]:
    """Apply calendar visibility rules: full/busy/hidden.

    Keyed by the calendar the event lives on (e.g. a shared/subscribed
    family calendar), falling back to the authenticated account.
    """
    visibility = settings.calendar_visibility
    filtered = []
    for e in events:
        vis = visibility.get(e.get("calendar")) or visibility.get(e.get("account"), "full")
        if vis == "hidden":
            continue
        if vis == "busy":
            e = {**e, "summary": "(busy)", "description": None, "location": None}
        filtered.append(e)
    return filtered


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
            pattern = f"%{term}%"
            term_filters.append(
                or_(
                    CalendarEvent.summary.ilike(pattern),
                    CalendarEvent.description.ilike(pattern),
                )
            )
        if term_filters:
            query = query.filter(or_(*term_filters))

    query = query.order_by(CalendarEvent.start_time)

    if limit:
        query = query.limit(limit)

    events = query.all()

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
    )
    events = query.all()

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
    return json.dumps(_apply_visibility(raw), indent=2)


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
    )

    if result is None:
        return json.dumps({"error": f"Failed to create event on {account}"})
    return json.dumps(result, indent=2)


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        {
            "name": "calendar_list_events",
            "description": (
                "List upcoming calendar events across all family calendars. "
                "Returns event title, time, location, and which calendar/account it's from. "
                "Defaults to the next 7 days. Optionally filter by account email or search text."
            ),
            "inputSchema": {
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
            "handler": handle_list_events,
            "category": "calendar",
            "examples": [
                "What's on this week?",
                "Any events next Thursday?",
                "Search for dentist appointments",
            ],
        },
        {
            "name": "calendar_today",
            "description": (
                "Show today's calendar events with times, locations, and which calendar "
                "they're from. Work events show as '(busy)' for privacy. "
                "Use this to check what's on today."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "handler": handle_today,
            "category": "calendar",
            "examples": [
                "What's on today?",
                "Do I have any meetings today?",
            ],
        },
        {
            "name": "calendar_next_events",
            "description": (
                "Get the next N upcoming events across all calendars, regardless of date. "
                "Useful for a quick glance at what's coming up soon."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of events to return (default 10)",
                        "default": 10,
                    },
                },
            },
            "handler": handle_next_events,
            "category": "calendar",
            "examples": [
                "What's coming up next?",
                "Show me the next 5 events",
            ],
        },
        {
            "name": "calendar_create_event",
            "description": (
                "Create a new event on a Google Calendar. Use for scheduling appointments, "
                "reminders with calendar entries, or blocking time. Defaults to the primary "
                "personal account (alex@example.com)."
            ),
            "inputSchema": {
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
                        "description": "Google account email to create event on (default: alex@example.com)",
                    },
                },
                "required": ["summary", "start_time"],
            },
            "handler": handle_create_event,
            "category": "calendar",
            "examples": [
                "Create a calendar event for the dentist on April 15th at 2pm",
                "Block time for house viewing on the 20th",
                "Add an all-day reminder for Sam's birthday",
            ],
        },
    ]
