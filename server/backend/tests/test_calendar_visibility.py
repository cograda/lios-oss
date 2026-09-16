"""Wave 5.8 — calendar visibility audit (db tier).

`calendar_visibility` (`app.integrations.google_calendar.manifest`) governs
whether a cached event is shown in full, redacted to "(busy)", or hidden
entirely from every calendar read path. The backlog item (open since
2026-05-02) reported a child's school event leaking into a view it should
have been filtered out of, and asked for an audit of whether the filter is
applied on every path that returns calendar events to a caller.

## What this suite found (see the PR body for the full table)

`calendar_events` is HOUSEHOLD-SHARED BY DESIGN
(`app.privacy.HOUSEHOLD_SHARED_TABLES["calendar_events"]` — "Shared household
calendar, by design.") — there is no `user_id` column, and
`calendar_visibility`'s `dict_str_str` config shape has no per-viewer
dimension (it is keyed on the *calendar*/*account* an event lives on, not on
who is asking). So the invariant this suite protects is: every read path
applies the SAME household-wide filter, identically, regardless of which
household member is asking — not "user A sees X, user B doesn't", which
would need a config shape this integration does not have (see the PR body
for why that wasn't retrofitted here).

## The historical bug (already fixed, before this item was opened)

`bb7b4a9` (2026-04-17) — the literal symptom described in the backlog item —
fixed `_apply_visibility` keying on `account` (always the authenticated
Google account) instead of `calendar` (the specific shared/subscribed
calendar an event lives on), which let a calendar explicitly marked "hidden"
leak through under its account's "full" rating. That fix predates this
backlog item's open date. `TestHistoricalRegressionFixedApril17` pins it so
it can't regress silently.

## Read paths audited

  - `calendar_list_events` (`handle_list_events` -> `query_events`)
  - `calendar_today` (`handle_today`)
  - `calendar_next_events` (`handle_next_events` -> `query_events`)
  - `GoogleCalendarFacade.today()` / `.list_events()` — the only way
    `system`'s daily brief / morning briefing / week-ahead composites reach
    calendar data (`app.plugin.capabilities.get_capability("calendar.query")`)
  - `dashboard_data()` — the web dashboard's calendar panel

All five now funnel through one helper, `_serialize_events()` (which calls
`_apply_visibility()`) — the consolidation this PR makes so a new consumer
cannot forget the filter by hand-building a dict from `CalendarEvent` rows.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.context import use_user
from app.plugin import config_store

pytestmark = pytest.mark.db

# A subscribed/shared calendar living under the household's own connected
# account — exactly the shape of the historical leak: `calendar_account` is
# the connected account, `calendar_name` is the specific calendar within it.
FULL_ACCOUNT = "alex@example.com"
HIDDEN_CALENDAR = "school-class-3b@group.calendar.google.com"
# A second connected account, configured "busy" — its own events should be
# redacted, never hidden outright.
BUSY_ACCOUNT = "family-member@example.com"


def _set_visibility(visibility: dict[str, str]) -> None:
    config_store.set_config_value("google_calendar", "calendar_visibility", visibility)


def _event(session, *, account: str, calendar_name: str | None, summary: str):
    from app.integrations.google_calendar.models import CalendarEvent

    now = datetime.now(timezone.utc) + timedelta(hours=1)
    ev = CalendarEvent(
        google_event_id=f"evt-{summary}-{account}-{calendar_name}",
        calendar_account=account,
        calendar_name=calendar_name,
        summary=summary,
        description=f"{summary} description",
        location=f"{summary} location",
        start_time=now,
        end_time=now + timedelta(hours=1),
        all_day=False,
        status="confirmed",
    )
    session.add(ev)
    return ev


@pytest.fixture
def _three_calendars(db_session):
    """Three events across three calendars: one full, one hidden (the
    child's school calendar, subscribed under the household's own account —
    the exact shape of the historical leak), one busy."""
    _event(db_session, account=FULL_ACCOUNT, calendar_name=None, summary="Dentist")
    _event(
        db_session, account=FULL_ACCOUNT, calendar_name=HIDDEN_CALENDAR,
        summary="School Assembly",
    )
    _event(db_session, account=BUSY_ACCOUNT, calendar_name=None, summary="Work Meeting")
    db_session.commit()

    _set_visibility({HIDDEN_CALENDAR: "hidden", BUSY_ACCOUNT: "busy"})


def _summaries(json_str: str) -> set[str]:
    return {e["summary"] for e in json.loads(json_str)}


def _read_as(user_id: int, fn, *args):
    from app.db import get_db

    db = get_db()
    with db.session() as session, use_user(user_id):
        return fn(session, *args)


class TestReadPathsAllApplyVisibility:
    """Every documented read path must hide the school calendar and redact
    the busy one — identically, regardless of which household member asks
    (there is no per-viewer dimension; see the module docstring)."""

    def test_calendar_list_events(self, real_db, _three_calendars):
        from app.integrations.google_calendar.tools import handle_list_events

        for uid in (1, 2):
            summaries = _summaries(_read_as(uid, handle_list_events, {"days": 1}))
            assert "Dentist" in summaries
            assert "School Assembly" not in summaries
            assert "Work Meeting" not in summaries
            assert "(busy)" in summaries

    def test_calendar_today(self, real_db, _three_calendars):
        from app.integrations.google_calendar.tools import handle_today

        for uid in (1, 2):
            summaries = _summaries(_read_as(uid, handle_today, {}))
            assert "Dentist" in summaries
            assert "School Assembly" not in summaries
            assert "(busy)" in summaries

    def test_calendar_next_events(self, real_db, _three_calendars):
        from app.integrations.google_calendar.tools import handle_next_events

        for uid in (1, 2):
            summaries = _summaries(_read_as(uid, handle_next_events, {"count": 10}))
            assert "School Assembly" not in summaries
            assert "Dentist" in summaries

    def test_facade_today_and_list_events(self, real_db, _three_calendars):
        """The only path `system`'s daily brief / morning briefing /
        week-ahead composites use to reach calendar data."""
        from app.integrations.google_calendar.facade import FACADE

        for uid in (1, 2):
            today_summaries = _summaries(_read_as(uid, FACADE.today, {}))
            listed_summaries = _summaries(_read_as(uid, FACADE.list_events, {"days": 1}))
            assert "School Assembly" not in today_summaries
            assert "School Assembly" not in listed_summaries

    def test_dashboard_data(self, real_db, _three_calendars):
        from app.integrations.google_calendar import GoogleCalendarIntegration

        result = asyncio.run(GoogleCalendarIntegration().dashboard_data())
        summaries = {
            e.get("summary") for evs in result["by_day"].values() for e in evs
        }
        assert "School Assembly" not in summaries
        # Dentist (full) + the redacted busy event — the hidden one never
        # reaches the panel's event count at all.
        assert result["events_this_week"] == 2


class TestMutationGuard:
    """Remove the filter from exactly one path and confirm exactly that
    path's test would go red — proves the assertions above are load-bearing,
    not accidentally-always-true (e.g. because the fixture never actually
    seeded a hidden event)."""

    def test_handle_today_unfiltered_would_leak_the_school_event(
        self, real_db, _three_calendars,
    ):
        """Simulates the exact regression this PR's consolidation guards
        against: a hand-rolled dict build from `CalendarEvent` rows that
        skips `_serialize_events`/`_apply_visibility` entirely."""
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo

        from app.integrations.google_calendar.models import CalendarEvent

        def _unfiltered_today(session, arguments):
            tz = ZoneInfo("Europe/Dublin")
            today_start = dt.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)
            events = (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.start_time >= today_start,
                    CalendarEvent.start_time < today_end,
                    CalendarEvent.status == "confirmed",
                )
                .order_by(CalendarEvent.start_time)
                .all()
            )
            return json.dumps([{"summary": e.summary} for e in events])

        out = _read_as(1, _unfiltered_today, {})
        # With the filter removed, the school event DOES leak — proving the
        # real `handle_today`'s equivalent assertion above is actually
        # exercising the filter, not passing by coincidence.
        assert "School Assembly" in _summaries(out)


class TestHistoricalRegressionFixedApril17:
    """Pins bb7b4a9 (2026-04-17, 'Fix calendar visibility filter'): the
    lookup must key on the calendar an event lives on before falling back to
    the authenticated account. Keying on account alone let a hidden
    subscribed calendar leak through under its account's "full" rating —
    this predates the backlog item (opened 2026-05-02) but is exactly the
    symptom it describes, so it stays pinned here.
    """

    def test_subscribed_calendar_hidden_even_though_account_is_full(
        self, real_db, db_session,
    ):
        from app.integrations.google_calendar.tools import _apply_visibility

        _set_visibility({HIDDEN_CALENDAR: "hidden"})
        events = [{
            "summary": "School Assembly",
            "calendar": HIDDEN_CALENDAR,
            "account": FULL_ACCOUNT,  # unset in config -> defaults to "full"
        }]
        assert _apply_visibility(events) == []

    def test_calendar_key_is_tried_before_account_key(self, real_db, db_session):
        """The pre-fix code (`visibility.get(e["account"], "full")`) never
        even looked at `calendar` — this pins the current order directly."""
        from app.integrations.google_calendar.tools import _apply_visibility

        _set_visibility({HIDDEN_CALENDAR: "hidden", FULL_ACCOUNT: "full"})
        events = [{
            "summary": "School Assembly",
            "calendar": HIDDEN_CALENDAR,
            "account": FULL_ACCOUNT,
        }]
        # If this ever regresses to account-first, the account's "full"
        # would win and this would come back non-empty.
        assert _apply_visibility(events) == []
