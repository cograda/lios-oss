"""Unit tests pinning google_calendar's behavior across its V4 chunk 4.1
conversion to `BidirectionalIntegration` (unit tier — mocked `list_events`
and a mock session, no real Postgres).

The critical behavior this file exists to protect: the cross-calendar
duplicate-event dedup added in a separate (already-committed) sam-rollout
change — when the same Google event id appears on multiple calendars (the
organizer's own primary calendar + a shared/subscribed calendar), the sync
must keep the copy from the account's own primary calendar
(`calendar_id == account_email`), regardless of `list_events()`'s (i.e.
`calendarList().list()`'s) iteration order. This must survive the
`sync_calendar()` -> `pull_calendar_events()`/`store_calendar_events()` split
unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.google_calendar.sync import pull_calendar_events, store_calendar_events
from app.plugin.bases import PullResult

pytestmark = pytest.mark.unit

ACCOUNT = "alex@example.com"


def _event(event_id: str, calendar_id: str, summary: str = "Event") -> dict:
    return {
        "google_event_id": event_id,
        "calendar_id": calendar_id,
        "calendar_account": ACCOUNT,
        "calendar_name": calendar_id,
        "summary": summary,
        "description": None,
        "location": None,
        "start_time": "2026-08-01T10:00:00Z",
        "end_time": "2026-08-01T11:00:00Z",
        "all_day": False,
        "status": "confirmed",
    }


class TestPullCalendarEventsDedup:
    """`pull_calendar_events` fetch + dedup, no DB writes."""

    def test_no_duplicates_passthrough(self):
        events = [_event("evt-1", ACCOUNT), _event("evt-2", "shared@family.com")]
        with patch(
            "app.integrations.google_calendar.sync.list_events", return_value=events,
        ):
            result = pull_calendar_events(ACCOUNT, MagicMock(), user_id=1)

        assert isinstance(result, PullResult)
        assert [e["google_event_id"] for e in result.records] == ["evt-1", "evt-2"]

    def test_primary_calendar_copy_wins_when_primary_listed_first(self):
        events = [
            _event("evt-1", ACCOUNT, summary="Primary copy"),
            _event("evt-1", "shared@family.com", summary="Shared copy"),
        ]
        with patch(
            "app.integrations.google_calendar.sync.list_events", return_value=events,
        ):
            result = pull_calendar_events(ACCOUNT, MagicMock(), user_id=1)

        assert len(result.records) == 1
        assert result.records[0]["calendar_id"] == ACCOUNT
        assert result.records[0]["summary"] == "Primary copy"

    def test_primary_calendar_copy_wins_when_shared_listed_first(self):
        """The dedup fix's whole point: order independence. If calendarList()
        happens to return the shared/subscribed calendar before the account's
        own primary calendar, the primary copy must still win."""
        events = [
            _event("evt-1", "shared@family.com", summary="Shared copy"),
            _event("evt-1", ACCOUNT, summary="Primary copy"),
        ]
        with patch(
            "app.integrations.google_calendar.sync.list_events", return_value=events,
        ):
            result = pull_calendar_events(ACCOUNT, MagicMock(), user_id=1)

        assert len(result.records) == 1
        assert result.records[0]["calendar_id"] == ACCOUNT
        assert result.records[0]["summary"] == "Primary copy"

    def test_neither_copy_on_primary_keeps_first_seen(self):
        """If an event never appears on the account's own primary calendar
        (e.g. Sam is only ever invited, never the organizer), dedup falls
        back to keeping whichever copy list_events() returned first — no
        crash, no silent drop."""
        events = [
            _event("evt-1", "shared-a@family.com", summary="Copy A"),
            _event("evt-1", "shared-b@family.com", summary="Copy B"),
        ]
        with patch(
            "app.integrations.google_calendar.sync.list_events", return_value=events,
        ):
            result = pull_calendar_events(ACCOUNT, MagicMock(), user_id=1)

        assert len(result.records) == 1
        assert result.records[0]["summary"] == "Copy A"

    def test_no_events_returns_empty_pull_result(self):
        with patch(
            "app.integrations.google_calendar.sync.list_events", return_value=[],
        ):
            result = pull_calendar_events(ACCOUNT, MagicMock(), user_id=1)
        assert result.records == []
        assert result.cursor is None


class TestStoreCalendarEvents:
    """`store_calendar_events` persistence, mocked session."""

    def test_empty_records_is_a_no_op(self):
        session = MagicMock()
        assert store_calendar_events(session, []) == 0
        session.query.assert_not_called()
        session.commit.assert_not_called()

    def test_inserts_new_event_and_prunes_stale(self):
        session = MagicMock()
        # No existing row for the upsert lookup.
        session.query.return_value.filter_by.return_value.first.return_value = None
        # Chainable filter(...).delete(...) for the stale-event prune.
        session.query.return_value.filter.return_value.delete.return_value = 0

        records = [_event("evt-1", ACCOUNT)]
        count = store_calendar_events(session, records)

        assert count == 1
        session.add.assert_called_once()
        session.commit.assert_called_once()
        session.query.return_value.filter.return_value.delete.assert_called_once()

    def test_updates_existing_event_in_place(self):
        session = MagicMock()
        existing = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = existing
        session.query.return_value.filter.return_value.delete.return_value = 0

        records = [_event("evt-1", ACCOUNT, summary="Updated title")]
        count = store_calendar_events(session, records)

        assert count == 1
        assert existing.summary == "Updated title"
        session.add.assert_not_called()  # existing row is mutated, not re-added
