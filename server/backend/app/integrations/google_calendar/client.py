"""Google Calendar API client — wraps the Google API for multiple accounts."""

import logging
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.auth.oauth import get_credentials
from app.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)


def _classify_http_error(exc: Exception, context: str) -> Exception:
    """Map a googleapiclient/network failure to TransientError or PermanentError.

    Returns the exception to raise (chained `from exc` by the caller) rather
    than raising directly, so callers can keep their own `raise ... from exc`.
    Auth failures that mean "needs re-auth" are already handled upstream by
    `get_credentials` (raises `NeedsReauthError` before the API call is ever
    made) — a 401/403 reaching this point means something else is wrong
    (API disabled, insufficient scope, etc.), still permanent but distinct.
    """
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        if status in (401, 403):
            return PermanentError(f"{context}: HTTP {status} ({exc.reason})")
        if status == 429 or (status is not None and status >= 500):
            return TransientError(f"{context}: HTTP {status} ({exc.reason})")
        return exc
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return TransientError(f"{context}: {exc}")
    return exc


def get_calendar_service(account_email: str, session: Session, *, user_id: int):
    """Build a Google Calendar API service for the given account.

    `user_id` scopes the OAuthToken lookup so Sam cannot write to Alex's
    calendar (and vice versa). Returns None if no valid credentials are stored.
    """
    creds = get_credentials(account_email, session, user_id=user_id)
    if creds is None:
        logger.warning(f"No credentials for {account_email}")
        return None

    return build("calendar", "v3", credentials=creds)


def list_events(
    account_email: str,
    session: Session,
    *,
    user_id: int,
    time_min: datetime | None = None,
    time_max: datetime | None = None,
    max_results: int = 100,
) -> list[dict]:
    """Fetch events from all calendars for an account."""
    service = get_calendar_service(account_email, session, user_id=user_id)
    if service is None:
        return []

    if time_min is None:
        time_min = datetime.now(timezone.utc)

    results = []

    # List all calendars for this account. Deliberately NOT swallowed like the
    # per-calendar events().list() below — a failure here means the whole
    # account is unreachable and the caller (sync_calendar → scheduler) needs
    # to know so it can retry (transient) or stop retrying (permanent).
    try:
        calendars = service.calendarList().list().execute()
    except Exception as exc:
        raise _classify_http_error(exc, f"calendarList for {account_email}") from exc

    for cal in calendars.get("items", []):
        cal_id = cal["id"]
        cal_name = cal.get("summary", cal_id)

        try:
            events_result = (
                service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat() if time_max else None,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )

            for event in events_result.get("items", []):
                start = event.get("start", {})
                end = event.get("end", {})

                results.append({
                    "google_event_id": event["id"],
                    "calendar_account": account_email,
                    "calendar_name": cal_name,
                    "summary": event.get("summary"),
                    "description": event.get("description"),
                    "location": event.get("location"),
                    "start_time": start.get("dateTime", start.get("date")),
                    "end_time": end.get("dateTime", end.get("date")),
                    "all_day": "date" in start,
                    "status": event.get("status", "confirmed"),
                })
        except Exception:
            logger.exception(f"Failed to fetch events from {cal_name} ({account_email})")

    return results


def create_event(
    account_email: str,
    session: Session,
    summary: str,
    start_time: str,
    *,
    user_id: int,
    end_time: str | None = None,
    all_day: bool = False,
    description: str | None = None,
    location: str | None = None,
    calendar_id: str = "primary",
) -> dict | None:
    """Create a calendar event on the given account.

    Args:
        start_time: ISO 8601 datetime or YYYY-MM-DD for all-day events.
        end_time: ISO 8601 datetime or YYYY-MM-DD. Defaults to start + 1 hour (timed) or + 1 day (all-day).
    """
    service = get_calendar_service(account_email, session, user_id=user_id)
    if service is None:
        return None

    body: dict = {"summary": summary}

    if description:
        body["description"] = description
    if location:
        body["location"] = location

    if all_day:
        body["start"] = {"date": start_time[:10]}
        if end_time:
            body["end"] = {"date": end_time[:10]}
        else:
            # All-day events: end = start + 1 day
            from datetime import date, timedelta
            d = date.fromisoformat(start_time[:10])
            body["end"] = {"date": (d + timedelta(days=1)).isoformat()}
    else:
        body["start"] = {"dateTime": start_time, "timeZone": "Europe/Dublin"}
        if end_time:
            body["end"] = {"dateTime": end_time, "timeZone": "Europe/Dublin"}
        else:
            # Default: 1 hour duration
            dt = datetime.fromisoformat(start_time)
            body["end"] = {
                "dateTime": (dt + timedelta(hours=1)).isoformat(),
                "timeZone": "Europe/Dublin",
            }

    try:
        event = service.events().insert(calendarId=calendar_id, body=body).execute()
        logger.info(f"Created event '{summary}' on {account_email}: {event.get('htmlLink')}")
        return {
            "id": event["id"],
            "summary": event.get("summary"),
            "start": event.get("start"),
            "end": event.get("end"),
            "link": event.get("htmlLink"),
        }
    except Exception:
        logger.exception(f"Failed to create event '{summary}' on {account_email}")
        return None
