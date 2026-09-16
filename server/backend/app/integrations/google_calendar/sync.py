"""Pull/store split for Google Calendar — V4 chunk 4.1.

Collapses the old hand-rolled `sync_calendar()` (poll -> dedup -> upsert ->
prune-stale, all in one function) into the two pieces
`app.plugin.bases.SourceIntegration.sync()` calls separately: `pull_calendar_events`
(fetch + dedup only, no DB writes) and `store_calendar_events` (persist only, no
outbound I/O). `GoogleCalendarIntegration.pull`/`.store` in `__init__.py` are
thin one-line adapters onto these.
"""

import logging
from datetime import datetime, timedelta, timezone

from dateutil.parser import parse as parse_date
from sqlalchemy.orm import Session

from app.integrations.google_calendar.client import list_events
from app.integrations.google_calendar.models import CalendarEvent
from app.plugin.bases import PullResult

logger = logging.getLogger(__name__)

DAYS_AHEAD = 30


def pull_calendar_events(
    account_email: str, session: Session, *, user_id: int, days_ahead: int = DAYS_AHEAD,
) -> PullResult:
    """Fetch events for the next N days for one account and dedup cross-calendar
    copies of the same event. No DB writes — see `store_calendar_events`.
    """
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days_ahead)

    events = list_events(
        account_email, session, user_id=user_id, time_min=now, time_max=time_max,
    )
    if not events:
        logger.info(f"No events found for {account_email}")
        return PullResult(records=[])

    # Google reuses the same event id across every calendar an event appears on
    # (organizer's calendar, every invitee's calendar). When the same id shows up
    # more than once below, keep only one copy — but always prefer the copy from
    # the account's own primary calendar (calendar_id == account_email) over a
    # subscribed/shared calendar, regardless of calendarList() iteration order.
    # Otherwise an invite Sam sends Alex could get filed under her calendar
    # name and silently disappear behind her calendar's visibility rule.
    events = sorted(events, key=lambda e: e.get("calendar_id") != account_email)

    deduped: list[dict] = []
    seen_ids: set[str] = set()
    for event_data in events:
        google_id = event_data["google_event_id"]
        if google_id in seen_ids:
            continue  # Same event from a different calendar — skip duplicate
        seen_ids.add(google_id)
        deduped.append(event_data)

    return PullResult(records=deduped)


def store_calendar_events(session: Session, records: list[dict]) -> int:
    """Upsert `records` (one account's deduped events) and prune events that
    no longer exist upstream, within the same account/date-range scope the
    old `sync_calendar()` used. A `records` of `[]` is a no-op — matches the
    old "no events found -> skip deletion too" short-circuit, rather than
    wiping every future event for the account.
    """
    if not records:
        return 0

    account_email = records[0]["calendar_account"]
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=DAYS_AHEAD)

    synced = 0
    seen_ids: set[str] = set()

    for event_data in records:
        google_id = event_data["google_event_id"]
        seen_ids.add(google_id)

        # Parse datetimes
        start_str = event_data["start_time"]
        end_str = event_data["end_time"]
        start_time = parse_date(start_str) if start_str else now
        end_time = parse_date(end_str) if end_str else start_time

        # Ensure timezone-aware
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)

        # Upsert
        existing = (
            session.query(CalendarEvent)
            .filter_by(google_event_id=google_id)
            .first()
        )

        if existing:
            existing.summary = event_data["summary"]
            existing.description = event_data["description"]
            existing.location = event_data["location"]
            existing.start_time = start_time
            existing.end_time = end_time
            existing.all_day = event_data["all_day"]
            existing.status = event_data["status"]
            existing.calendar_name = event_data["calendar_name"]
        else:
            session.add(CalendarEvent(
                google_event_id=google_id,
                calendar_account=account_email,
                calendar_name=event_data["calendar_name"],
                summary=event_data["summary"],
                description=event_data["description"],
                location=event_data["location"],
                start_time=start_time,
                end_time=end_time,
                all_day=event_data["all_day"],
                status=event_data["status"],
            ))

        synced += 1

    # Remove events that no longer exist upstream (for this account, in the date range)
    session.query(CalendarEvent).filter(
        CalendarEvent.calendar_account == account_email,
        CalendarEvent.start_time >= now,
        CalendarEvent.start_time <= time_max,
        CalendarEvent.google_event_id.not_in(seen_ids) if seen_ids else False,
    ).delete(synchronize_session="fetch")

    session.commit()
    logger.info(f"Synced {synced} events for {account_email}")
    return synced
