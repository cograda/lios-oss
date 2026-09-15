"""Tests for alert signals that were technically true and practically useless.

Three findings from 2026-08-19, all the same shape: a signal that cannot
distinguish two situations with different owners, and so gets tuned out.

  - "data stale" meant both "our sync is broken" and "the source stopped
     producing". `lastfm` was investigated as a comar fault twice; both times
     comar was perfectly in sync and the Scrobbler app had stopped submitting.
  - `low_battery` mixed a phone at 1% (ordinary life) with a door sensor at 1%
     (a monitoring outage about to happen quietly).
  - `ha_entities` had no first-seen column, so +144 entities in 24h was
     unattributable — the only artefact was a count.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.homeassistant.tools import _is_personal_device_battery


class TestPersonalDeviceBatteryDetection:
    """Structural detection, not a list of device names — a name list would fail
    the personalisation guard and would need editing on every handset change."""

    # Mirrors the live registry: the companion app creates `_battery_state` on
    # the same stem as `_battery_level`, and nothing else does.
    REGISTRY = {
        "sensor.alexs_iphone_battery_level",
        "sensor.alexs_iphone_battery_state",
        "sensor.alexs_iphone_watch_battery_level",
        "sensor.alexs_iphone_watch_battery_state",
        "sensor.sams_iphone_battery_level",
        "sensor.sams_iphone_battery_state",
        "sensor.myggbett_door_window_sensor_battery",
        "sensor.sonoff_hydro_duo_battery",
        "sensor.comar_rack_ups_battery_charge",
        "sensor.dreamy_battery_level",
        "binary_sensor.master_blind_left_battery",
    }

    @pytest.mark.parametrize("entity_id", [
        "sensor.alexs_iphone_battery_level",
        "sensor.alexs_iphone_watch_battery_level",
        "sensor.sams_iphone_battery_level",
    ])
    def test_phones_and_watches_are_personal(self, entity_id):
        assert _is_personal_device_battery(entity_id, self.REGISTRY) is True

    @pytest.mark.parametrize("entity_id", [
        "sensor.myggbett_door_window_sensor_battery",
        "sensor.sonoff_hydro_duo_battery",
        "sensor.comar_rack_ups_battery_charge",
        "sensor.dreamy_battery_level",
        "binary_sensor.master_blind_left_battery",
    ])
    def test_hardware_is_not_personal(self, entity_id):
        """These are the ones whose battery dying costs data — they must keep
        reaching the alerting list."""
        assert _is_personal_device_battery(entity_id, self.REGISTRY) is False

    def test_unknown_battery_defaults_to_alerting(self):
        """Conservative direction. Mistaking hardware for a phone would silence a
        real outage; mistaking a phone for hardware only adds noise."""
        assert _is_personal_device_battery("sensor.mystery_thing_battery", set()) is False

    def test_a_battery_level_suffix_is_stripped_to_find_the_sibling(self):
        """The stem must lose `_battery_level` before the sibling lookup.

        Note this does *not* pin suffix ordering — a mutation reversing
        `_BATTERY_SUFFIXES` changes nothing, because no entry is an `endswith`
        suffix of another (`_battery_level` ends in `_level`). Claiming otherwise
        would be a comment describing a constraint that isn't there.
        """
        registry = {"sensor.foo_battery_level", "sensor.foo_battery_state"}
        assert _is_personal_device_battery("sensor.foo_battery_level", registry) is True

    def test_a_plain_battery_suffix_also_resolves(self):
        registry = {"sensor.bar_battery", "sensor.bar_charger_type"}
        assert _is_personal_device_battery("sensor.bar_battery", registry) is True

    def test_a_domainless_id_does_not_crash(self):
        assert _is_personal_device_battery("garbage", {"garbage"}) is False


@pytest.mark.db
class TestUpstreamDryIsDistinctFromBrokenSync:
    """A succeeding sync that returns nothing is not a fault in this system, and
    must not read like one."""

    # 200h (8.3 days) clears the lastfm threshold, which was raised from 48h to
    # 7d on 2026-08-19 after measuring that the largest real listening gap is
    # 72h. A test pinned to just-over-48h would silently stop exercising the
    # staleness branch the moment the threshold moved — which is exactly what
    # happened when it did.
    @pytest.fixture(autouse=True)
    def _registered(self):
        """Populate the integration registry.

        `_scheduled_integrations()` intersects the *registry* with manifests
        carrying a `schedule`, deliberately: an integration that is disabled has
        no cron running, so "the sync ran fine and found nothing" would be just
        as false for it as for a push-only one. That makes this check
        registry-dependent, and the registry is empty in a bare test process.
        """
        from app.integrations import register_all

        register_all()

    def _payload(self, db_session, *, status, failures, age_hours):
        import json

        from app.integrations.system.tools import handle_alerts_household
        from app.models.tokens import SyncState
        from app.integrations.lastfm.models import Scrobble

        db_session.add(SyncState(
            integration="lastfm",
            last_sync_status=status,
            last_sync_at=datetime.now(timezone.utc),
            consecutive_failures=failures,
        ))
        db_session.add(Scrobble(
            user_id=1,
            artist_name="Overmono",
            track_name="Slowmotion",
            played_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        ))
        db_session.commit()
        return json.loads(handle_alerts_household(db_session, {}))

    def _lastfm_issues(self, payload):
        for alert in payload.get("alerts", []):
            if alert["integration"] == "lastfm":
                return alert["issues"]
        return []

    def test_healthy_sync_with_stale_data_names_the_upstream(self, db_session):
        payload = self._payload(db_session, status="ok", failures=0, age_hours=200)
        issues = [i for i in self._lastfm_issues(payload) if "data stale" in i]
        assert issues, "expected a staleness issue"
        assert "no new data at the source" in issues[0]

    def test_error_status_alone_blocks_the_upstream_claim(self, db_session):
        """`last_sync_status` must be checked independently of the failure count.

        ⚠️ Added after a mutation neutering the status check passed: the other
        scenario set `consecutive_failures=3`, so the count guard alone rejected
        it and the status guard was never exercised. A run that ended `timeout`
        or `error` without yet incrementing the counter is exactly the case this
        guard exists for.
        """
        payload = self._payload(db_session, status="timeout", failures=0, age_hours=200)
        issues = [i for i in self._lastfm_issues(payload) if "data stale" in i]
        assert issues
        assert "no new data at the source" not in issues[0]

    def test_a_push_integration_never_claims_upstream_dry(self, db_session):
        """`apple_reminders` has `schedule=None` — nothing pulls, so a green
        SyncState is not evidence of a healthy fetch.

        ⚠️ Regression test for a defect this very change shipped. The first
        deploy rendered "data stale for Sam — sync is healthy, so there is no
        new data at the source" while her daemon was dead and writes were being
        *lost*. Exactly inverted. Found by live verification, not by the suite.
        """
        import json

        from app.integrations.system.tools import handle_alerts_household
        from app.models.tokens import SyncState
        from app.models.users import User

        db_session.add(SyncState(
            integration="apple_reminders",
            last_sync_status="ok",
            last_sync_at=datetime.now(timezone.utc),
            consecutive_failures=0,
        ))
        # apple_reminders probes the core User table (user_column="id"), so a
        # stale `reminders_verified_at` is what trips its probe.
        user = db_session.query(User).filter_by(id=1).first()
        if user is not None:
            user.reminders_verified_at = datetime.now(timezone.utc) - timedelta(days=2)
        db_session.commit()

        payload = json.loads(handle_alerts_household(db_session, {}))
        for alert in payload.get("alerts", []):
            if alert["integration"] != "apple_reminders":
                continue
            for issue in alert["issues"]:
                assert "no new data at the source" not in issue, issue

    def test_failing_sync_does_not_blame_the_upstream(self, db_session):
        """When the sync is actually broken, pointing at the source would send
        the investigation to the wrong place — the mirror of the original bug."""
        payload = self._payload(db_session, status="error", failures=3, age_hours=200)
        issues = [i for i in self._lastfm_issues(payload) if "data stale" in i]
        assert issues
        assert "no new data at the source" not in issues[0]


@pytest.mark.db
class TestEntityFirstSeenIsNotAHeartbeat:
    """`synced_at` is bumped on every sync, which is why the +144 delta could not
    be attributed. `first_seen_at` must never move."""

    def test_first_seen_survives_a_resync(self, db_session):
        from app.integrations.homeassistant.models import HAEntity

        row = HAEntity(entity_id="sensor.probe_one", domain="sensor", state="1")
        db_session.add(row)
        db_session.commit()
        original_first_seen = row.first_seen_at
        assert original_first_seen is not None

        # Simulate what sync.py does on every pass.
        row.state = "2"
        row.synced_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db_session.commit()
        db_session.refresh(row)

        assert row.first_seen_at == original_first_seen
        assert row.synced_at != row.first_seen_at
