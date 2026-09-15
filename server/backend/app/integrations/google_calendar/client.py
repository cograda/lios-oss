"""Google Calendar API client — wraps the Google API for multiple accounts."""

import logging
from datetime import datetime, timedelta, timezone

from dateutil.parser import parse as parse_date
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.auth.oauth import get_credentials
from app.errors import PermanentError
from app.integrations.google_calendar.models import CalendarEvent
from app.plugin.sync_runtime import classify_exc

logger = logging.getLogger(__name__)

# Auth failures that mean "needs re-auth" are already handled upstream by
# get_credentials (raises NeedsReauthError before the API call is ever
# made) — a 401/403 reaching this point means something else is wrong (API
# disabled, insufficient scope, etc.), still permanent but distinct. Override
# keeps classify_exc's oauth-provider default (NeedsReauthError) from firing
# a second time here.
_OVERRIDES = {401: PermanentError, 403: PermanentError}


def _classify(exc: Exception, context: str) -> Exception:
    return classify_exc(exc, context, provider="google", overrides=_OVERRIDES)


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
        raise _classify(exc, f"calendarList for {account_email}") from exc

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
                    "calendar_id": cal_id,
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
    attendees: list[str] | None = None,
    send_invites: bool = True,
) -> dict | None:
    """Create a calendar event on the given account.

    Args:
        start_time: ISO 8601 datetime or YYYY-MM-DD for all-day events.
        end_time: ISO 8601 datetime or YYYY-MM-DD. Defaults to start + 1 hour (timed) or + 1 day (all-day).
        attendees: Email addresses to invite.
        send_invites: Whether to email the attendees. Defaults True, because the
            Google API's own default is `sendUpdates="none"` — attaching
            attendees without it produces an event they are never told about,
            which looks like an invitation was sent when none was.
    """
    service = get_calendar_service(account_email, session, user_id=user_id)
    if service is None:
        return None

    body: dict = {"summary": summary}

    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]

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
        event = (
            service.events()
            .insert(
                calendarId=calendar_id,
                body=body,
                # Explicit even when there are no attendees: the API default is
                # "none", so this is the difference between inviting someone and
                # merely listing them.
                sendUpdates="all" if (attendees and send_invites) else "none",
            )
            .execute()
        )
        invited = [a.get("email") for a in event.get("attendees") or []]
        logger.info(
            f"Created event '{summary}' on {account_email}: {event.get('htmlLink')}"
            + (f" — invited {len(invited)}, notified={bool(send_invites)}" if invited else "")
        )
        return {
            "id": event["id"],
            "summary": event.get("summary"),
            "start": event.get("start"),
            "end": event.get("end"),
            "link": event.get("htmlLink"),
            # Echoed back so the caller can confirm who was actually attached
            # and whether they were told — never inferred from the request.
            "attendees": invited,
            "invites_sent": bool(invited and send_invites),
        }
    except Exception:
        logger.exception(f"Failed to create event '{summary}' on {account_email}")
        return None


def _reject_if_recurring(event: dict, *, event_id: str, account_email: str, calendar_id: str) -> None:
    """Decision C: refuse to touch a recurring master (`recurrence` set) or a
    recurring instance (`recurringEventId` set) rather than guessing whether
    the caller meant "this occurrence" or "the whole series". Google Calendar
    exposes both concepts through the same event id space, and silently
    picking one would be a worse failure mode than a clear refusal.
    """
    if event.get("recurrence") or event.get("recurringEventId"):
        raise PermanentError(
            f"Event {event_id} on {account_email}/{calendar_id} is part of a "
            "recurring series (has 'recurrence' or 'recurringEventId'). "
            "Updating/deleting recurring events is not supported by this tool "
            "yet — edit the series or occurrence directly in Google Calendar."
        )


def _sync_cached_row_after_update(session: Session, event_id: str, updated: dict, changed_fields: set[str]) -> None:
    """Decision F: keep the local `calendar_events` cache consistent immediately
    rather than leaving it stale until the next */15 sync poll — a user who just
    changed an event's time would otherwise still see the old time in
    `calendar_today`/`calendar_list_events` for up to 15 minutes. Only touches
    columns whose corresponding body field was actually part of the PATCH
    (`changed_fields`), matching the merge semantics of the API call itself.
    """
    cached = session.query(CalendarEvent).filter_by(google_event_id=event_id).first()
    if cached is None:
        return

    if "summary" in changed_fields:
        cached.summary = updated.get("summary")
    if "description" in changed_fields:
        cached.description = updated.get("description")
    if "location" in changed_fields:
        cached.location = updated.get("location")
    if "start" in changed_fields:
        start = updated.get("start", {})
        start_str = start.get("dateTime", start.get("date"))
        if start_str:
            start_time = parse_date(start_str)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            cached.start_time = start_time
            cached.all_day = "date" in start
    if "end" in changed_fields:
        end = updated.get("end", {})
        end_str = end.get("dateTime", end.get("date"))
        if end_str:
            end_time = parse_date(end_str)
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
            cached.end_time = end_time

    session.commit()


def update_event(
    account_email: str,
    session: Session,
    event_id: str,
    *,
    user_id: int,
    calendar_id: str = "primary",
    summary: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    all_day: bool | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    send_invites: bool = True,
) -> dict | None:
    """Update fields on an existing calendar event, leaving everything else
    untouched (decision B: uses `events().patch()`, never `events().update()`
    — `update()` is a PUT that replaces the whole event body, so a caller
    who only wants to fix a typo in the title would otherwise silently wipe
    the description, location, attendees, etc. Only arguments explicitly
    passed (not None) end up in the PATCH body).

    `account_email` and `calendar_id` are both required (decision A): an
    event id alone is ambiguous across 4+ personal accounts plus a work
    account, each with multiple calendars, so this never searches for the
    event — it looks it up on exactly the calendar named.

    Recurring events (decision C) are refused outright — see
    `_reject_if_recurring`.

    Args:
        start_time: ISO 8601 datetime, or YYYY-MM-DD if `all_day` is true.
            If given without `end_time`, only the start moves — pass both if
            the whole event should shift by the same amount; this function
            does not infer a new end time the way `create_event` does for a
            brand-new event.
        all_day: Only needed when changing between timed/all-day; otherwise
            inferred from whether `start_time` contains "T".
        send_invites: Whether Google emails existing/updated attendees about
            the change. Defaults True — unlike `create_event`, the API's own
            default here is also to notify, so this mirrors that rather than
            overriding it.
    """
    service = get_calendar_service(account_email, session, user_id=user_id)
    if service is None:
        return None

    try:
        existing = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except Exception as exc:
        raise _classify(
            exc, f"fetch event {event_id} before update on {account_email}/{calendar_id}"
        ) from exc

    _reject_if_recurring(existing, event_id=event_id, account_email=account_email, calendar_id=calendar_id)

    body: dict = {}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if attendees is not None:
        body["attendees"] = [{"email": e} for e in attendees]

    if start_time is not None or end_time is not None:
        is_all_day = all_day if all_day is not None else (start_time is not None and "T" not in start_time)
        if is_all_day:
            if start_time is not None:
                body["start"] = {"date": start_time[:10]}
            if end_time is not None:
                body["end"] = {"date": end_time[:10]}
        else:
            if start_time is not None:
                body["start"] = {"dateTime": start_time, "timeZone": "Europe/Dublin"}
            if end_time is not None:
                body["end"] = {"dateTime": end_time, "timeZone": "Europe/Dublin"}

    if not body:
        raise PermanentError("update_event called with no fields to change")

    try:
        updated = (
            service.events()
            .patch(
                calendarId=calendar_id,
                eventId=event_id,
                body=body,
                sendUpdates="all" if send_invites else "none",
            )
            .execute()
        )
    except Exception as exc:
        raise _classify(
            exc, f"update event {event_id} on {account_email}/{calendar_id}"
        ) from exc

    _sync_cached_row_after_update(session, event_id, updated, set(body.keys()))

    invited = [a.get("email") for a in updated.get("attendees") or []]
    logger.info(f"Updated event {event_id} on {account_email}: {updated.get('htmlLink')}")
    return {
        "id": updated["id"],
        "summary": updated.get("summary"),
        "start": updated.get("start"),
        "end": updated.get("end"),
        "link": updated.get("htmlLink"),
        "attendees": invited,
        "invites_sent": bool(invited and send_invites),
        "fields_changed": sorted(body.keys()),
    }


def delete_event(
    account_email: str,
    session: Session,
    event_id: str,
    *,
    user_id: int,
    calendar_id: str = "primary",
    send_updates: bool = True,
) -> dict | None:
    """Delete a calendar event, returning the detail of what was removed
    (decision D) so the caller can show the user what actually got deleted —
    Google's trash window is finite, so this never pretends the action is
    freely reversible. Fetches the event first: a bad/unknown event id
    raises a clear classified error (decision E/G — same `_classify` path as
    `create_event`/`update_event`) rather than the delete call silently
    no-op'ing on a 404.

    `account_email` and `calendar_id` are required for the same reason as
    `update_event` (decision A) — no cross-account/cross-calendar search.

    Recurring events (decision C) are refused outright — see
    `_reject_if_recurring`.
    """
    service = get_calendar_service(account_email, session, user_id=user_id)
    if service is None:
        return None

    try:
        existing = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except Exception as exc:
        raise _classify(
            exc, f"fetch event {event_id} before delete on {account_email}/{calendar_id}"
        ) from exc

    _reject_if_recurring(existing, event_id=event_id, account_email=account_email, calendar_id=calendar_id)

    detail = {
        "id": existing.get("id", event_id),
        "summary": existing.get("summary"),
        "start": existing.get("start"),
        "end": existing.get("end"),
        "account": account_email,
        "calendar_id": calendar_id,
    }

    try:
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates="all" if send_updates else "none",
        ).execute()
    except Exception as exc:
        raise _classify(
            exc, f"delete event {event_id} on {account_email}/{calendar_id}"
        ) from exc

    # Decision F: remove the cached row immediately rather than letting a
    # deleted event keep showing up in calendar_today/calendar_list_events
    # for up to 15 minutes until the next sync poll prunes it.
    cached = session.query(CalendarEvent).filter_by(google_event_id=event_id).first()
    if cached is not None:
        session.delete(cached)
        session.commit()

    logger.info(f"Deleted event {event_id} ('{detail['summary']}') on {account_email}")
    return detail
