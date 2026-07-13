"""Sync logic: poll Google Calendar API → upsert into Postgres."""

import logging
from datetime import datetime, timedelta, timezone

from dateutil.parser import parse as parse_date
from sqlalchemy.orm import Session

from app.integrations.google_calendar.client import list_events
from app.integrations.google_calendar.models import CalendarEvent

logger = logging.getLogger(__name__)


def sync_calendar(
    account_email: str, session: Session, *, user_id: int, days_ahead: int = 30
) -> int:
    """Fetch events for the next N days and upsert into the DB.

    Returns the number of events synced.
    """
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days_ahead)

    events = list_events(
        account_email, session, user_id=user_id, time_min=now, time_max=time_max,
    )
    if not events:
        logger.info(f"No events found for {account_email}")
        return 0

    synced = 0
    seen_ids = set()

    for event_data in events:
        google_id = event_data["google_event_id"]
        if google_id in seen_ids:
            continue  # Same event from a different calendar — skip duplicate
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
