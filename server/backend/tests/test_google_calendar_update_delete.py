"""Unit tests for calendar_update_event / calendar_delete_event (unit tier —
mocked Google API service and a mocked session, no real Postgres).

Covers:
  - update_event uses PATCH semantics: only fields explicitly passed reach
    the request body (decision B) — unspecified fields are left untouched.
  - delete_event fetches the event first and returns captured detail
    (title/time/calendar) rather than a bare confirmation (decision D).
  - A bad/unknown event id (404 from the Google API) raises a clear typed
    error (PermanentError) rather than the client silently no-op'ing.
  - The account (+ calendar id) routing requirement: the tool schemas
    require `account`/`event_id`, and the handlers reject a missing account
    before ever touching the Google API (decision A).
  - The recurring-event restriction: an event carrying `recurrence` or
    `recurringEventId` is refused outright rather than mutated (decision C).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.auth.context import use_user
from app.errors import PermanentError
from app.integrations.google_calendar.client import delete_event, update_event
from app.integrations.google_calendar.tools import (
    get_mcp_tools,
    handle_delete_event,
    handle_update_event,
)

pytestmark = pytest.mark.unit

ACCOUNT = "alex@example.com"
CALENDAR_ID = "primary"
EVENT_ID = "evt-123"


def _http_error(status: int) -> HttpError:
    resp = httplib2.Response({"status": str(status)})
    return HttpError(resp, b"error body")


class FakeExecutable:
    """Mimics the Google API client's `.execute()` chain end-point."""

    def __init__(self, result=None, error: Exception | None = None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class FakeEventsResource:
    """Mimics `service.events()` — enough of the surface for get/patch/delete."""

    def __init__(self, get_event: dict | None = None, get_error: Exception | None = None):
        self._get_event = get_event
        self._get_error = get_error
        self.patch_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.patch_result: dict | None = None
        self.delete_error: Exception | None = None

    def get(self, calendarId, eventId):
        return FakeExecutable(result=self._get_event, error=self._get_error)

    def patch(self, calendarId, eventId, body, sendUpdates=None):
        self.patch_calls.append(
            {"calendarId": calendarId, "eventId": eventId, "body": body, "sendUpdates": sendUpdates}
        )
        result = self.patch_result if self.patch_result is not None else {**self._get_event, **body}
        return FakeExecutable(result=result)

    def delete(self, calendarId, eventId, sendUpdates=None):
        self.delete_calls.append(
            {"calendarId": calendarId, "eventId": eventId, "sendUpdates": sendUpdates}
        )
        return FakeExecutable(result=None, error=self.delete_error)


class FakeService:
    def __init__(self, events_resource: FakeEventsResource):
        self._events_resource = events_resource

    def events(self):
        return self._events_resource


def _existing_event(**overrides) -> dict:
    base = {
        "id": EVENT_ID,
        "summary": "Dentist",
        "description": "Bring insurance card",
        "location": "Main St Clinic",
        "start": {"dateTime": "2026-08-15T14:00:00+01:00", "timeZone": "Europe/Dublin"},
        "end": {"dateTime": "2026-08-15T15:00:00+01:00", "timeZone": "Europe/Dublin"},
        "htmlLink": "https://calendar.google.com/event?eid=abc",
    }
    base.update(overrides)
    return base


def _mock_session() -> MagicMock:
    """A session whose `.query(CalendarEvent)...` chain returns no cached row
    by default — most tests here don't care about the local-cache side effect.
    """
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = None
    return session


# ---------------------------------------------------------------------------
# client.update_event — PATCH semantics
# ---------------------------------------------------------------------------


class TestUpdateEventPatchSemantics:
    def test_only_passed_fields_reach_the_patch_body(self):
        events_resource = FakeEventsResource(get_event=_existing_event())
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            result = update_event(
                ACCOUNT, _mock_session(), EVENT_ID, user_id=1, description="New notes",
            )

        assert len(events_resource.patch_calls) == 1
        body = events_resource.patch_calls[0]["body"]
        assert body == {"description": "New notes"}
        assert result["fields_changed"] == ["description"]

    def test_unspecified_fields_are_preserved_not_wiped(self):
        """The whole point of PATCH over PUT: fields the caller never
        mentioned (location, in this case) must still be present afterwards."""
        events_resource = FakeEventsResource(get_event=_existing_event())
        events_resource.patch_result = _existing_event(summary="Dentist (moved)")
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            result = update_event(
                ACCOUNT, _mock_session(), EVENT_ID, user_id=1, summary="Dentist (moved)",
            )

        body = events_resource.patch_calls[0]["body"]
        assert body == {"summary": "Dentist (moved)"}
        assert "location" not in body
        assert "description" not in body
        assert result["summary"] == "Dentist (moved)"

    def test_uses_patch_not_update(self):
        """No `update` (PUT) method exists on the fake resource at all —
        if client.update_event ever called `.update()` instead of
        `.patch()`, this test would AttributeError."""
        events_resource = FakeEventsResource(get_event=_existing_event())
        assert not hasattr(events_resource, "update")
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            update_event(ACCOUNT, _mock_session(), EVENT_ID, user_id=1, summary="X")

        assert len(events_resource.patch_calls) == 1

    def test_no_fields_given_raises_permanent_error(self):
        events_resource = FakeEventsResource(get_event=_existing_event())
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            with pytest.raises(PermanentError):
                update_event(ACCOUNT, _mock_session(), EVENT_ID, user_id=1)


# ---------------------------------------------------------------------------
# client.delete_event — captured detail
# ---------------------------------------------------------------------------


class TestDeleteEventCapturedDetail:
    def test_returns_title_time_calendar_before_deleting(self):
        events_resource = FakeEventsResource(get_event=_existing_event())
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            result = delete_event(ACCOUNT, _mock_session(), EVENT_ID, user_id=1)

        assert result["summary"] == "Dentist"
        assert result["start"] == {"dateTime": "2026-08-15T14:00:00+01:00", "timeZone": "Europe/Dublin"}
        assert result["account"] == ACCOUNT
        assert result["calendar_id"] == CALENDAR_ID
        assert len(events_resource.delete_calls) == 1
        assert events_resource.delete_calls[0]["eventId"] == EVENT_ID

    def test_removes_matching_cached_row(self):
        events_resource = FakeEventsResource(get_event=_existing_event())
        service = FakeService(events_resource)

        session = MagicMock()
        cached_row = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = cached_row

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            delete_event(ACCOUNT, session, EVENT_ID, user_id=1)

        session.delete.assert_called_once_with(cached_row)
        session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Unknown event id -> clear typed error
# ---------------------------------------------------------------------------


class TestUnknownEventId:
    def test_update_raises_on_404(self):
        events_resource = FakeEventsResource(get_error=_http_error(404))
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            with pytest.raises(PermanentError):
                update_event(ACCOUNT, _mock_session(), EVENT_ID, user_id=1, summary="X")

    def test_delete_raises_on_404_rather_than_no_op(self):
        events_resource = FakeEventsResource(get_error=_http_error(404))
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            with pytest.raises(PermanentError):
                delete_event(ACCOUNT, _mock_session(), EVENT_ID, user_id=1)

        # Nothing was ever deleted — the failure happened at the fetch step.
        assert events_resource.delete_calls == []

    def test_403_on_readonly_calendar_flows_through_classification(self):
        """decision G: no special-case pre-check for read-only/non-owned
        calendars — a 403 from Google just flows through the same
        `_classify` path as everything else, into a clear PermanentError."""
        events_resource = FakeEventsResource(get_event=_existing_event())
        events_resource.delete_error = _http_error(403)
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            with pytest.raises(PermanentError):
                delete_event(ACCOUNT, _mock_session(), EVENT_ID, user_id=1)


# ---------------------------------------------------------------------------
# Recurring-event restriction
# ---------------------------------------------------------------------------


class TestRecurringEventRestriction:
    def test_update_refuses_recurring_master(self):
        events_resource = FakeEventsResource(
            get_event=_existing_event(recurrence=["RRULE:FREQ=WEEKLY"])
        )
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            with pytest.raises(PermanentError, match="recurring"):
                update_event(ACCOUNT, _mock_session(), EVENT_ID, user_id=1, summary="X")

        assert events_resource.patch_calls == []

    def test_update_refuses_recurring_instance(self):
        events_resource = FakeEventsResource(
            get_event=_existing_event(recurringEventId="series-abc")
        )
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            with pytest.raises(PermanentError, match="recurring"):
                update_event(ACCOUNT, _mock_session(), EVENT_ID, user_id=1, summary="X")

    def test_delete_refuses_recurring_master(self):
        events_resource = FakeEventsResource(
            get_event=_existing_event(recurrence=["RRULE:FREQ=WEEKLY"])
        )
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            with pytest.raises(PermanentError, match="recurring"):
                delete_event(ACCOUNT, _mock_session(), EVENT_ID, user_id=1)

        assert events_resource.delete_calls == []


# ---------------------------------------------------------------------------
# Account routing requirement
# ---------------------------------------------------------------------------


class TestAccountRoutingRequirement:
    def test_tool_schemas_require_account_and_event_id(self):
        tools = {t["name"]: t for t in get_mcp_tools()}
        for name in ("calendar_update_event", "calendar_delete_event"):
            required = tools[name]["inputSchema"]["required"]
            assert "account" in required
            assert "event_id" in required

    def test_handle_update_event_rejects_missing_account(self):
        with use_user(1):
            result = handle_update_event(_mock_session(), {"event_id": EVENT_ID, "summary": "X"})
        assert "error" in result
        assert "account" in result

    def test_handle_delete_event_rejects_missing_account(self):
        with use_user(1):
            result = handle_delete_event(_mock_session(), {"event_id": EVENT_ID})
        assert "error" in result
        assert "account" in result

    def test_handle_update_event_rejects_account_not_owned_by_caller(self):
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        with use_user(1):
            result = handle_update_event(
                session, {"account": ACCOUNT, "event_id": EVENT_ID, "summary": "X"},
            )
        assert "not owned" in result

    def test_handle_delete_event_never_calls_google_api_without_owned_account(self):
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
        ) as mock_get_service:
            with use_user(1):
                handle_delete_event(session, {"account": ACCOUNT, "event_id": EVENT_ID})
            mock_get_service.assert_not_called()

    def test_handle_update_event_succeeds_when_account_owned(self):
        session = MagicMock()
        owned_token = MagicMock()
        session.query.return_value.filter_by.return_value.first.side_effect = [
            owned_token,  # OAuthToken ownership check
            None,  # CalendarEvent cache lookup inside update_event
        ]

        events_resource = FakeEventsResource(get_event=_existing_event())
        service = FakeService(events_resource)

        with patch(
            "app.integrations.google_calendar.client.get_calendar_service",
            return_value=service,
        ):
            with use_user(1):
                result_json = handle_update_event(
                    session, {"account": ACCOUNT, "event_id": EVENT_ID, "summary": "New title"},
                )

        assert "error" not in result_json or "\"error\"" not in result_json
        assert len(events_resource.patch_calls) == 1
        assert events_resource.patch_calls[0]["body"] == {"summary": "New title"}
