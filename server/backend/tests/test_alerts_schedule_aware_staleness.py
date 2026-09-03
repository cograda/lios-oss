"""system_alerts axis 1 (SyncState staleness) is schedule-aware.

The bug: a flat `threshold_minutes` cutoff had no notion of an integration's
own cron schedule, so `commute` — whose sync only runs 07:00-08:57 weekday
mornings (`"0-57 7-8 * * 1-5"`) — alarmed with "sync stale" every afternoon
and all through any non-scheduled day, forever. That's not a fault, it's the
integration correctly not running outside its window.

The fix (`app.services.data_freshness.is_sync_overdue`, wired into
`app/integrations/system/tools.py::_build_alerts_payload` axis 1) asks
whether the integration's *own* schedule implied another run was due since
the last success, using the same `CronTrigger.from_crontab` construction as
the real scheduler (`app/scheduler.py`) — not a hand-rolled per-integration
special case.

These tests exercise the real end-to-end path (`handle_alerts_household`,
the entry point the notifications sweep and dashboard actually call) rather
than just the helper function (see `tests/test_data_freshness.py` for that),
with wall-clock time frozen by monkeypatching the `datetime` name imported
into `app.integrations.system.tools`.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _registered_integrations():
    """Axis 1's schedule-aware check (`_scheduled_integrations()`) reads the
    live `INTEGRATIONS` registry, not just the manifest — so, matching the
    pattern used by `test_manifests.py`/`test_sync_contract.py`/etc., it
    must actually be populated or `commute`/`lastfm` silently aren't
    'scheduled' and every assertion below would pass vacuously."""
    from app.integrations import INTEGRATIONS, register_all

    register_all()
    assert INTEGRATIONS, "expected register_all() to populate the registry"


class _FrozenDatetime(datetime):
    """A `datetime` subclass whose `.now()` returns a fixed instant.

    Swapped in for the module-level `datetime` name in
    `app.integrations.system.tools` so every `datetime.now(timezone.utc)`
    call in `_build_alerts_payload` (the cutoff, and the schedule-aware
    overdue check) sees the same frozen "now" — without a freezegun
    dependency, which this codebase doesn't otherwise use.
    """

    _frozen: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen


def _freeze_now(monkeypatch, when: datetime) -> None:
    import app.integrations.system.tools as tools_module

    _FrozenDatetime._frozen = when
    monkeypatch.setattr(tools_module, "datetime", _FrozenDatetime)


def _seed_sync_state(
    session, *, integration: str, last_sync_at: datetime, status: str = "ok",
    consecutive_failures: int = 0,
) -> None:
    from app.models.tokens import SyncState

    session.add(SyncState(
        integration=integration,
        last_sync_status=status,
        consecutive_failures=consecutive_failures,
        last_sync_at=last_sync_at,
    ))
    session.commit()


def _issues_for(payload: dict, integration: str) -> list[str]:
    entry = next((a for a in payload["alerts"] if a["integration"] == integration), None)
    return entry["issues"] if entry else []


class TestCommuteWindowScheduleAwareness:
    """`commute` is the live case that motivated this fix."""

    def test_outside_its_window_does_not_alarm(self, db_session, monkeypatch):
        """The literal reported bug, reproduced with the exact evidence
        timestamps: `last_sync_at` 2026-08-13T07:57:00Z (08:57 Dublin, the
        last tick of the morning window) checked at 13:49 the same
        afternoon — ~5h52m later. A flat cutoff alone would alarm; the
        schedule says nothing was due until tomorrow morning."""
        from app.integrations.system.tools import handle_alerts_household

        last_sync = datetime(2026, 8, 13, 7, 57, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, 12, 49, tzinfo=timezone.utc)
        _seed_sync_state(db_session, integration="commute", last_sync_at=last_sync)
        _freeze_now(monkeypatch, now)

        payload = json.loads(handle_alerts_household(db_session, {}))
        assert not any(
            issue.startswith("sync stale") for issue in _issues_for(payload, "commute")
        )

    def test_overdue_within_its_own_window_still_alarms(self, db_session, monkeypatch):
        """True positive: still within a day the schedule actually runs on
        (2026-08-13 is a Thursday — one of the schedule's real scheduled
        days; see `tests/test_data_freshness.py` for the day-of-week
        numbering this depends on), well past the default 60-minute
        threshold, with the schedule ticking every minute since. Must
        still alarm — the fix must not silence real staleness."""
        from app.integrations.system.tools import handle_alerts_household

        last_sync = datetime(2026, 8, 13, 6, 25, tzinfo=timezone.utc)  # 07:25 Dublin
        now = datetime(2026, 8, 13, 7, 30, tzinfo=timezone.utc)  # 08:30 Dublin
        _seed_sync_state(db_session, integration="commute", last_sync_at=last_sync)
        _freeze_now(monkeypatch, now)

        payload = json.loads(handle_alerts_household(db_session, {}))
        assert any(
            issue.startswith("sync stale") for issue in _issues_for(payload, "commute")
        )

    def test_over_a_non_scheduled_gap_does_not_alarm(self, db_session, monkeypatch):
        """Last success on the schedule's last running day before a gap of days
        it never runs on, checked during the gap — must not alarm.

        Rewritten 2026-08-13. The original used a Saturday sync checked on the
        Monday, which passed only because the manifest's `1-5` was silently
        firing Tue-Sat under APScheduler's 0=Monday numbering. Once the manifest
        was corrected to `mon-fri`, Saturday stopped being a running day and the
        Monday 07:00-08:57 window *had* passed unsynced, so alarming became the
        right answer and this test correctly failed. The intent is unchanged;
        the dates now express it against real weekday semantics.
        """
        from app.integrations.system.tools import handle_alerts_household

        # Fri 14 Aug 08:57 Dublin — the end of the last real run window.
        last_sync = datetime(2026, 8, 14, 7, 57, tzinfo=timezone.utc)
        # Sun 16 Aug: the next due run is Monday morning, so nothing is overdue.
        now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        _seed_sync_state(db_session, integration="commute", last_sync_at=last_sync)
        _freeze_now(monkeypatch, now)

        payload = json.loads(handle_alerts_household(db_session, {}))
        assert not any(
            issue.startswith("sync stale") for issue in _issues_for(payload, "commute")
        )


class TestContinuousScheduleUnaffected:
    """A continuous cron (`lastfm`, "*/15 * * * *") must alarm exactly as
    before — the schedule-aware check must not make it *harder* to detect
    real staleness for the common case."""

    def test_lastfm_still_alarms_when_genuinely_stale(self, db_session, monkeypatch):
        from app.integrations.system.tools import handle_alerts_household

        now = datetime(2026, 8, 13, 12, 49, tzinfo=timezone.utc)
        last_sync = now - timedelta(minutes=70)  # several missed */15 ticks
        _seed_sync_state(db_session, integration="lastfm", last_sync_at=last_sync)
        _freeze_now(monkeypatch, now)

        payload = json.loads(handle_alerts_household(db_session, {}))
        assert any(
            issue.startswith("sync stale") for issue in _issues_for(payload, "lastfm")
        )

    def test_lastfm_does_not_alarm_shortly_after_syncing(self, db_session, monkeypatch):
        from app.integrations.system.tools import handle_alerts_household

        now = datetime(2026, 8, 13, 12, 49, tzinfo=timezone.utc)
        last_sync = now - timedelta(minutes=5)
        _seed_sync_state(db_session, integration="lastfm", last_sync_at=last_sync)
        _freeze_now(monkeypatch, now)

        payload = json.loads(handle_alerts_household(db_session, {}))
        assert not any(
            issue.startswith("sync stale") for issue in _issues_for(payload, "lastfm")
        )


class TestPushFedIntegrationsUnaffected:
    """`apple_health`/`apple_reminders` have `schedule=None` — no cron, fed
    by pushes instead. Axis 1 must keep skipping the "sync stale" check for
    them entirely, exactly as before this fix (they were never in
    `_scheduled_integrations()` to begin with)."""

    def test_apple_health_never_gets_sync_stale_regardless_of_age(self, db_session, monkeypatch):
        from app.integrations.system.tools import handle_alerts_household

        now = datetime(2026, 8, 13, 12, 49, tzinfo=timezone.utc)
        last_sync = now - timedelta(days=30)
        _seed_sync_state(db_session, integration="apple_health", last_sync_at=last_sync)
        _freeze_now(monkeypatch, now)

        payload = json.loads(handle_alerts_household(db_session, {}))
        assert not any(
            issue.startswith("sync stale") for issue in _issues_for(payload, "apple_health")
        )
