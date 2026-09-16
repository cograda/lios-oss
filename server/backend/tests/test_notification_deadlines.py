"""Tests for `notifications/deadlines.py` — the sleep-by-10am watch.

The interesting surface is the *time* logic, not the query. A deadline check
has two ways to be useless and they pull in opposite directions: firing before
the deadline (when the export legitimately hasn't run yet, i.e. every night
while you're asleep) and never firing at all. Both are pinned below.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.integrations.notifications import deadlines

DUBLIN = ZoneInfo("Europe/Dublin")


def _config(**overrides):
    base = {
        "sleep_deadline_user_ids": ["1"],
        "sleep_deadline_hour": 10,
        "sleep_deadline_timezone": "Europe/Dublin",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _health(hours):
    """Stand-in for the `health.query` capability. `hours=None` means the night
    has no session rows at all, which is the condition being watched for."""
    return SimpleNamespace(slept_hours=lambda session, *, user_id, night: hours)


@pytest.fixture
def wired(monkeypatch):
    def _wire(*, hours, **cfg):
        monkeypatch.setattr(deadlines, "plugin_config", lambda name: _config(**cfg))
        monkeypatch.setattr(deadlines, "get_capability", lambda name: _health(hours))
    return _wire


def _at(hour, minute=0, day=19):
    return datetime(2026, 8, day, hour, minute, tzinfo=DUBLIN)


class TestNightDue:
    """`_night_due` is the whole time decision, isolated."""

    def test_before_the_deadline_nothing_is_due(self):
        assert deadlines._night_due(_at(8), 10) is None

    def test_at_the_deadline_hour_the_night_is_due(self):
        assert deadlines._night_due(_at(10), 10) == date(2026, 8, 19)

    def test_after_the_deadline_the_night_is_due(self):
        assert deadlines._night_due(_at(13, 30), 10) == date(2026, 8, 19)

    def test_late_evening_closes_the_window(self):
        """Past `_WINDOW_END_HOUR` the alert is no longer actionable, and
        leaving it open would fire its recovery ping in the small hours."""
        assert deadlines._night_due(_at(23), 10) is None

    def test_the_night_is_the_date_sleep_ended_on(self):
        """Matches `apple_health.sleep_window`'s bucketing: a night belongs to
        the date it ends on, so at 10am on the 19th the night in question is
        the 19th, not the 18th."""
        assert deadlines._night_due(_at(10), 10) == date(2026, 8, 19)


class TestCollect:
    def test_missing_sleep_past_the_deadline_raises_one_item(self, wired, mock_session):
        wired(hours=None)
        items = deadlines.collect(mock_session, now=_at(10, 5))
        assert len(items) == 1
        assert items[0].target_user_id == 1
        assert items[0].fingerprint == "deadline:sleep:1:2026-08-19"

    def test_present_sleep_raises_nothing(self, wired, mock_session):
        wired(hours=7.55)
        assert deadlines.collect(mock_session, now=_at(10, 5)) == []

    def test_zero_hours_is_data_not_absence(self, wired, mock_session):
        """A night recorded as entirely awake is a real measurement. Treating
        it as missing would alert on the one night the export definitely
        worked — the same unknown-vs-broken conflation this module avoids."""
        wired(hours=0.0)
        assert deadlines.collect(mock_session, now=_at(10, 5)) == []

    def test_before_the_deadline_raises_nothing_even_when_missing(self, wired, mock_session):
        """The load-bearing case. Sleep data is genuinely absent at 03:00 every
        single night; a staleness threshold cannot tell that apart from a
        failure, and a deadline can."""
        wired(hours=None)
        assert deadlines.collect(mock_session, now=_at(3)) == []

    def test_unconfigured_runs_no_checks(self, wired, mock_session):
        wired(hours=None, sleep_deadline_user_ids=[])
        assert deadlines.collect(mock_session, now=_at(10, 5)) == []

    def test_fingerprint_carries_the_night(self, wired, mock_session):
        """Two consecutive missed mornings must be two episodes. A fingerprint
        without the date would leave yesterday's row open, and the ledger's
        resend window would swallow today's alert."""
        wired(hours=None)
        today = deadlines.collect(mock_session, now=_at(10, 5, day=19))[0]
        tomorrow = deadlines.collect(mock_session, now=_at(10, 5, day=20))[0]
        assert today.fingerprint != tomorrow.fingerprint

    def test_multiple_users_each_get_their_own_item(self, wired, mock_session):
        wired(hours=None, sleep_deadline_user_ids=["1", "2"])
        items = deadlines.collect(mock_session, now=_at(10, 5))
        assert {i.target_user_id for i in items} == {1, 2}

    def test_bad_hour_disables_rather_than_guesses(self, wired, mock_session):
        wired(hours=None, sleep_deadline_hour=99)
        assert deadlines.collect(mock_session, now=_at(10, 5)) == []

    def test_junk_user_id_is_skipped_not_fatal(self, wired, mock_session):
        wired(hours=None, sleep_deadline_user_ids=["nope", "1"])
        items = deadlines.collect(mock_session, now=_at(10, 5))
        assert [i.target_user_id for i in items] == [1]

    def test_bad_timezone_falls_back_rather_than_raising(self, wired, mock_session):
        """A typo'd timezone must not take the whole sweep down — and must not
        silently become UTC-with-no-warning either; the fallback is explicit."""
        wired(hours=None, sleep_deadline_timezone="Mars/Olympus_Mons")
        items = deadlines.collect(mock_session, now=_at(10, 5))
        assert len(items) == 1

    def test_one_users_lookup_failure_does_not_lose_the_other(self, monkeypatch, mock_session):
        monkeypatch.setattr(
            deadlines, "plugin_config",
            lambda name: _config(sleep_deadline_user_ids=["1", "2"]),
        )

        def _flaky(session, *, user_id, night):
            if user_id == 1:
                raise RuntimeError("db blip")
            return None

        monkeypatch.setattr(
            deadlines, "get_capability",
            lambda name: SimpleNamespace(slept_hours=_flaky),
        )
        items = deadlines.collect(mock_session, now=_at(10, 5))
        assert [i.target_user_id for i in items] == [2]

    def test_body_names_the_night_and_the_time(self, wired, mock_session):
        """The message has to be actionable on a lock screen: which night, how
        late it is, and what to check."""
        wired(hours=None)
        body = deadlines.collect(mock_session, now=_at(10, 5))[0].body
        assert "Tue 18 Aug" in body
        assert "10:05" in body
