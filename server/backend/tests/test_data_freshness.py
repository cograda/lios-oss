"""Data freshness probe tests (unit tier).

Focused on the commute probe's window-gating (Chunk E item 2): the scheduled
solve only runs weekday 07:00-08:57 Europe/Dublin, so the freshness axis must
not evaluate — let alone alarm on — the commute table outside that window.

Also covers the homeassistant probe (Chunk F): it must track WS-listener
liveness, not HAEntity.synced_at, which the 5-min poll stamps on every
entity regardless of whether the WS listener is alive.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services import data_freshness as df


class TestCommuteWindowActive:
    def test_weekday_within_window(self):
        # 2026-07-13 is a Monday. Ireland is on BST (UTC+1) in July, so
        # 07:30 UTC == 08:30 Dublin — inside 07:00-08:57.
        now = datetime(2026, 7, 13, 7, 30, tzinfo=timezone.utc)
        assert df._commute_window_active(now) is True

    def test_weekday_before_window(self):
        now = datetime(2026, 7, 13, 5, 0, tzinfo=timezone.utc)  # 06:00 Dublin
        assert df._commute_window_active(now) is False

    def test_weekday_after_window(self):
        now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)  # 09:00 Dublin
        assert df._commute_window_active(now) is False

    def test_weekend_never_active(self):
        # 2026-07-11 is a Saturday, same clock time as the Monday case.
        now = datetime(2026, 7, 11, 7, 30, tzinfo=timezone.utc)
        assert df._commute_window_active(now) is False


class TestCheckAllCommuteGating:
    def test_commute_excluded_outside_window(self):
        session = MagicMock()
        with (
            patch.object(df, "_commute_window_active", return_value=False),
            patch.object(df, "_probe", return_value=None),
        ):
            results = df.check_all(session)
        assert "commute" not in {r.integration for r in results}

    def test_commute_included_inside_window(self):
        session = MagicMock()
        with (
            patch.object(df, "_commute_window_active", return_value=True),
            patch.object(df, "_probe", return_value=None),
        ):
            results = df.check_all(session)
        assert "commute" in {r.integration for r in results}

    def test_other_integrations_unaffected_by_commute_gating(self):
        """The window check is scoped to the "commute" name only — every
        other integration is probed regardless of the commute window."""
        session = MagicMock()
        with (
            patch.object(df, "_commute_window_active", return_value=False),
            patch.object(df, "_probe", return_value=None),
        ):
            results = df.check_all(session)
        names = {r.integration for r in results}
        # Frozen fixture — the old DATA_FRESHNESS_THRESHOLDS dict, before it
        # was replaced by manifest-driven `staleness_probe` (chunk 1.3).
        # Not a live import of deleted code.
        old_probed_names = {
            "whatsapp",
            "google_mail",
            "lastfm",
            "apple_health",
            "apple_reminders",
            "homeassistant",
            "commute",
        }
        # `per_user` probes (2026-08-17) emit one row *per owner*, so against a mock
        # session with no owners they contribute zero rows — they are still probed,
        # which `TestManifestDrivenProbeSet` below asserts on the probe registry
        # itself. This assertion is about commute gating, so it checks the
        # table-wide probes only rather than re-asserting the whole set.
        per_user = {n for n, p in df._staleness_probes().items() if p.per_user}
        assert per_user, "expected at least one per-user probe to exist"
        # Probes added since the frozen fixture above (see
        # TestManifestDrivenProbeSet.ADDED_PROBES — same reasoning: the fixture
        # is a historical record, additions are listed, not merged in).
        added = {"solar_forecast"}
        assert names == (old_probed_names | added) - {"commute"} - per_user


class TestManifestDrivenProbeSet:
    """Chunk 1.3: the probed set + thresholds now come from each
    integration's manifest (`staleness_probe`), not a hand-maintained dict.
    This pins them against a frozen copy of the old
    `DATA_FRESHNESS_THRESHOLDS` dict (deleted in this chunk) so a manifest
    edit that silently changes probing behavior fails a test."""

    OLD_DATA_FRESHNESS_THRESHOLDS = {
        "whatsapp": 24 * 3600,
        "google_mail": 6 * 3600,
        "lastfm": 48 * 3600,
        "apple_health": 36 * 3600,
        "apple_reminders": 5 * 60,
        "homeassistant": 15 * 60,
        "commute": 6 * 60,
    }

    # Deliberate post-migration divergences from the frozen baseline above.
    # The baseline stays untouched as the historical record; anything that
    # changes without an entry here is still caught as silent drift.
    DELIBERATE_CHANGES = {
        # 2026-07-29: 6 min was a false positive by construction — the commute
        # job only runs 07:00-08:57 weekdays, so a healthy integration alerted
        # ~22h/day and all weekend. 72h spans Friday 08:57 -> Monday 07:00.
        "commute": 72 * 3600,
        # 2026-08-19: 48h sat *inside* the normal distribution. Measured over
        # the last 4,000 scrobbles (back to 2025-11-29): 48 gaps of >=24h, 4 of
        # >=48h, largest exactly 72.0h. So the old threshold fired roughly four
        # times a year on a quiet weekend, which is why it was investigated as a
        # fault on 13 and 19 August — both times comar was correctly in sync.
        #
        # `played_at` is when a track was played, not when it was ingested, and
        # the scrobbler submits in batches: on 19 Aug it flushed two days of
        # plays at once, during which Last.fm's own API genuinely did not have
        # them. So the threshold must cover the largest listening gap (72h)
        # *plus* submission lag. 7 days does, and still catches the only thing
        # this probe uniquely detects — a permanently dead scrobbler. A broken
        # sync is SyncState's job.
        "lastfm": 7 * 24 * 3600,
        # 2026-08-19/27: 5 minutes was a false positive by construction — a
        # MacBook with the lid closed overnight is indistinguishable from a
        # dead daemon at 5 minutes, and `apple_reminders:data_stale` flapped
        # every 30-60 minutes around the clock (vault/Projects/lios/
        # Backlog.md, "Push notifications flap all night"). Also now
        # config-overridable (`reminders_stale_minutes`) rather than a bare
        # constant — see apple_reminders/manifest.py.
        "apple_reminders": 540 * 60,
    }

    # Probes added since the frozen baseline. Kept separate from
    # OLD_DATA_FRESHNESS_THRESHOLDS so that dict stays what it says it is — a
    # historical record — while anything appearing without an entry here is
    # still caught as silent drift.
    ADDED_PROBES = {
        # 2026-08-22, with the algo harness. 6h = three missed prediction
        # cycles on a 20-past-the-hour schedule. Deliberately not tighter: the
        # lesson from commute's own threshold is that a probe firing during a
        # schedule's normal quiet periods trains everyone to ignore
        # system_alerts.
        "solar_forecast": 6 * 3600,
    }

    def test_probed_set_unchanged(self):
        expected = set(self.OLD_DATA_FRESHNESS_THRESHOLDS) | set(self.ADDED_PROBES)
        assert set(df._staleness_probes()) == expected

    def test_added_probe_thresholds(self):
        for name, seconds in self.ADDED_PROBES.items():
            assert df._staleness_probes()[name].threshold_minutes * 60 == seconds, name

    def test_every_deriver_probe_is_narrowed_to_its_own_rows(self):
        """Derivers share one `algo_predictions` table, so an unfiltered
        MAX(made_at) reports the freshest row across all of them — one live
        forecaster masking a dead one. Structurally the same failure `per_user`
        exists to prevent, and this catches the second deriver forgetting it."""
        for name, probe in df._staleness_probes().items():
            if probe.model == "AlgoPrediction":
                assert probe.filter_column == "algo", name
                assert probe.filter_value == name, name

    def test_thresholds_unchanged(self):
        for name, old_seconds in self.OLD_DATA_FRESHNESS_THRESHOLDS.items():
            expected = self.DELIBERATE_CHANGES.get(name, old_seconds)
            probe = df._staleness_probes()[name]
            assert probe.threshold_minutes * 60 == expected, name


class TestUnmeasuredIntegrations:
    """`unmeasured_integrations()` — the "honest numbers" hardening pass.

    `check_all` only reports on the 7 integrations with a `staleness_probe`
    in their manifest; the other ~19 are simply absent from its output, which
    reads identically to "measured and healthy". This is the function that
    lets `system_alerts` name the gap instead of hiding it.
    """

    def test_returns_every_unprobed_integration(self):
        # Every manifest name minus the probed set, computed independently of
        # `unmeasured_integrations`'s own implementation so this doesn't just
        # restate the function under test.
        from app.plugin.validate import discover_manifests

        all_names = set(discover_manifests())
        probed_names = set(df._staleness_probes())
        expected = all_names - probed_names

        result = df.unmeasured_integrations()
        assert set(result) == expected
        # Sanity floor: this was 19 of 26 when written. A future integration
        # gaining or losing a probe is fine; the set collapsing to ~0 or
        # growing to "everything" would mean the derivation broke.
        assert len(expected) > 10

    def test_probed_integrations_are_not_unmeasured(self):
        result = set(df.unmeasured_integrations())
        for name in df._staleness_probes():
            assert name not in result, name

    def test_commute_never_listed_even_though_it_has_a_probe(self):
        # commute HAS a staleness_probe — it's merely window-gated out of
        # check_all() most of the day. That's "probed but out of window",
        # not "never measured", and the two must not collapse into one list.
        assert "commute" not in df.unmeasured_integrations()
        assert "commute" in df._staleness_probes()

    def test_known_unprobed_integration_is_listed(self):
        # finance has no staleness_probe at all (manual-only sync) and is
        # the concrete example from the incident that motivated this fix —
        # 143 days of no data reading as an all-green panel.
        assert "finance" in df.unmeasured_integrations()


class TestScheduleAwareOverdueCheck:
    """`next_expected_run`/`is_sync_overdue` — the general mechanism behind
    the fix for system_alerts axis 1 (`app/integrations/system/tools.py`):
    "has this integration's own cron schedule implied a run was due since
    it last succeeded", instead of a flat cutoff blind to the schedule's
    shape. Exercises both a continuous cron (`*/15 * * * *`, e.g. lastfm)
    and a weekday-morning window (`0-57 7-8 * * 1-5`, e.g. commute) — the
    exact schedule that produced the false alarm this fixes.
    """

    COMMUTE_SCHEDULE = "0-57 7-8 * * mon-fri"
    COMMUTE_TZ = "Europe/Dublin"
    CONTINUOUS_SCHEDULE = "*/15 * * * *"

    # `CronTrigger.from_crontab` (used here and by the real scheduler in
    # `app/scheduler.py`) hands the day-of-week field straight to APScheduler's
    # own `CronTrigger`, whose numbering is 0=Monday..6=Sunday (matching
    # `date.weekday()`) — *not* Vixie-cron's 0=Sunday..6=Saturday, despite
    # `from_crontab` advertising crontab syntax.
    #
    # Found 2026-08-13 while writing these tests: commute's manifest said
    # "1-5", meaning Monday-Friday, and was therefore firing **Tue-Sat** — no
    # Monday sync (a commute day) and a pointless Saturday one, live since the
    # integration was written. The manifest now uses named days, which say what
    # they mean regardless of the numbering. The two tests below pin both halves
    # so neither can regress silently.

    def _fire_days(self, schedule: str) -> set[str]:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(schedule, timezone=self.COMMUTE_TZ)
        origin = datetime(2026, 8, 9, tzinfo=timezone.utc)  # a Sunday
        cutoff = origin + timedelta(days=9)
        cur, prev, seen = origin, None, set()
        for _ in range(1000):
            nxt = trigger.get_next_fire_time(previous_fire_time=prev, now=cur)
            if nxt is None or nxt > cutoff:
                break
            seen.add(nxt.strftime("%A"))
            prev = cur = nxt
        return seen

    def test_named_days_mean_what_they_say(self):
        """The live manifest form. Guards the fix itself: if this ever returns
        Tue-Sat again, commute has silently lost its Monday sync."""
        assert self._fire_days(self.COMMUTE_SCHEDULE) == {
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        }

    def test_numeric_day_of_week_is_still_offset_by_one(self):
        """Pins the upstream quirk that caused the bug, so an APScheduler
        upgrade which fixes (or worsens) it fails loudly here rather than
        quietly changing which mornings anything runs on. Independent of our
        manifests — nothing in the codebase should use numeric day-of-week."""
        assert self._fire_days("0-57 7-8 * * 1-5") == {
            "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
        }

    def test_no_manifest_uses_a_numeric_day_of_week_field(self):
        """The regression guard that matters. A numeric day-of-week in any
        manifest cron is off by one against author intent, so ban the form
        rather than relying on everyone remembering the quirk."""
        from app.plugin.validate import discover_manifests

        offenders = []
        for name, manifest in discover_manifests().items():
            crons = [manifest.schedule] + [
                spec.cron for spec in (manifest.background_tasks or []) if spec.cron
            ]
            for cron in [c for c in crons if c]:
                fields = cron.split()
                if len(fields) >= 5 and fields[4] != "*" and any(
                    ch.isdigit() for ch in fields[4]
                ):
                    offenders.append(f"{name}: {cron!r}")
        assert not offenders, (
            "use named days (mon-fri) — numeric day-of-week is 0=Monday in "
            f"APScheduler and reads as off-by-one: {offenders}"
        )

    def test_continuous_schedule_overdue_after_missed_tick(self):
        # Last synced 70 minutes ago on a */15 schedule — several ticks
        # missed, clearly overdue.
        now = datetime(2026, 8, 13, 13, 49, tzinfo=timezone.utc)
        last_sync = now - timedelta(minutes=70)
        assert df.is_sync_overdue(last_sync, now, self.CONTINUOUS_SCHEDULE, None) is True

    def test_continuous_schedule_not_overdue_moments_after_sync(self):
        # Synced 1 minute ago — the next */15 tick hasn't arrived yet.
        now = datetime(2026, 8, 13, 13, 49, tzinfo=timezone.utc)
        last_sync = now - timedelta(minutes=1)
        assert df.is_sync_overdue(last_sync, now, self.CONTINUOUS_SCHEDULE, None) is False

    def test_window_schedule_not_overdue_outside_window(self):
        """The literal false-alarm this fixes: commute last synced at the
        tail of its morning window, checked mid-afternoon the same day —
        the schedule has no next tick until tomorrow morning, so this must
        not be reported as overdue."""
        # 2026-08-13 is a Thursday. Ireland is on BST (UTC+1) in August, so
        # the window's last tick, 08:57 Dublin, is 07:57 UTC — matching the
        # live evidence's `last_sync_at: 2026-08-13T07:57:00Z` exactly.
        last_sync = datetime(2026, 8, 13, 7, 57, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, 12, 49, tzinfo=timezone.utc)  # 13:49 Dublin
        assert (
            df.is_sync_overdue(last_sync, now, self.COMMUTE_SCHEDULE, self.COMMUTE_TZ)
            is False
        )

    def test_window_schedule_not_overdue_across_a_non_scheduled_gap(self):
        # Last successful run at the end of Friday's window, checked on the
        # Sunday: Saturday and Sunday are not scheduled days, so no tick has
        # been due in between and nothing is overdue.
        #
        # Originally written as Saturday sync / Monday check, which only held
        # because the manifest's "1-5" was silently firing Tue-Sat. With named
        # days the Monday window is real, so that pair now *should* alarm.
        last_sync = datetime(2026, 8, 14, 7, 57, tzinfo=timezone.utc)  # Fri 08:57 Dublin
        now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)  # Sunday
        assert (
            df.is_sync_overdue(last_sync, now, self.COMMUTE_SCHEDULE, self.COMMUTE_TZ)
            is False
        )

    def test_window_schedule_overdue_within_its_own_window(self):
        """True positive: still inside the weekday-morning window, but the
        last successful sync was long enough ago that the schedule implies
        several missed ticks — must still alarm."""
        # Thursday 07:25 Dublin == 06:25 UTC; checked at 08:30 Dublin == 07:30 UTC.
        last_sync = datetime(2026, 8, 13, 6, 25, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, 7, 30, tzinfo=timezone.utc)
        assert (
            df.is_sync_overdue(last_sync, now, self.COMMUTE_SCHEDULE, self.COMMUTE_TZ)
            is True
        )

    def test_grace_period_absorbs_the_instant_a_tick_becomes_due(self):
        # Last sync exactly one tick ago on a */15 schedule; "now" is only a
        # few seconds past when the next tick became due — inside the grace
        # window, so not yet overdue.
        last_sync = datetime(2026, 8, 13, 13, 0, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, 13, 15, 10, tzinfo=timezone.utc)
        assert df.is_sync_overdue(last_sync, now, self.CONTINUOUS_SCHEDULE, None) is False

    def test_naive_datetimes_assumed_utc(self):
        """`last_sync_at` comes back naive from some SQLAlchemy configs —
        must not raise, and must behave as if it were UTC."""
        last_sync = datetime(2026, 8, 13, 6, 25)  # naive
        now = datetime(2026, 8, 13, 7, 30, tzinfo=timezone.utc)
        assert (
            df.is_sync_overdue(last_sync, now, self.COMMUTE_SCHEDULE, self.COMMUTE_TZ)
            is True
        )


class _FakeListener:
    def __init__(self, last_event_at):
        self.last_event_at = last_event_at
        self.connected = True


class TestHomeAssistantProbe:
    """The exact regression this chunk fixes: the old probe used
    max(HAEntity.synced_at), which the 5-min poll stamps on every entity
    every cycle regardless of WS-listener health — so it only ever caught a
    total HA outage. It must now track WS-listener liveness instead."""

    def test_probe_uses_ws_heartbeat_not_entity_synced_at(self):
        """A session whose HAEntity table would report a fresh synced_at
        must NOT be queried at all — the probe should go straight to the
        WS heartbeat and never touch the DB for this integration."""
        session = MagicMock()
        stale_ws_ts = datetime.now(timezone.utc) - timedelta(minutes=45)
        with patch(
            "app.integrations.homeassistant.events.current_listener",
            _FakeListener(stale_ws_ts),
        ):
            latest = df._probe(session, "homeassistant")

        assert latest == stale_ws_ts
        session.query.assert_not_called()

    def test_check_all_fires_when_ws_stale_despite_healthy_poll(self):
        """The exact case the old probe missed: poll `synced_at` is fresh
        (entities upserted every 5 min regardless), but the WS listener has
        gone quiet well past the threshold — the probe must flag this as
        stale."""
        session = MagicMock()
        now = datetime.now(timezone.utc)
        stale_ws_ts = now - timedelta(minutes=45)  # >> 15 min threshold
        with (
            patch.object(df, "_commute_window_active", return_value=False),
            patch(
                "app.integrations.homeassistant.events.current_listener",
                _FakeListener(stale_ws_ts),
            ),
        ):
            results = df.check_all(session)

        ha_result = next(r for r in results if r.integration == "homeassistant")
        assert ha_result.age_seconds is not None
        assert ha_result.age_seconds > ha_result.threshold_seconds

    def test_check_all_does_not_fire_when_ws_healthy(self):
        session = MagicMock()
        now = datetime.now(timezone.utc)
        fresh_ws_ts = now - timedelta(minutes=2)
        with (
            patch.object(df, "_commute_window_active", return_value=False),
            patch(
                "app.integrations.homeassistant.events.current_listener",
                _FakeListener(fresh_ws_ts),
            ),
        ):
            results = df.check_all(session)

        ha_result = next(r for r in results if r.integration == "homeassistant")
        assert ha_result.age_seconds is not None
        assert ha_result.age_seconds <= ha_result.threshold_seconds

    def test_probe_none_when_listener_never_started(self):
        session = MagicMock()
        with patch(
            "app.integrations.homeassistant.events.current_listener", None
        ):
            assert df._probe(session, "homeassistant") is None


@pytest.mark.db
class TestPerUserProbes:
    """A table-wide `MAX()` over per-user data reports the *freshest* owner, so
    one working device masks another that has stopped.

    Measured live 2026-08-17, both reported healthy:
      - `apple_reminders` read 0m — Alex's daemon — while Sam's had been silent
        46 minutes against what was then a 5-minute threshold (raised to 540
        minutes on 2026-08-27 — see apple_reminders/manifest.py — so these
        tests now use a silence long enough to exceed the current threshold).
      - `apple_health` read 5h44m — Sam's phone — while Alex's was 9h10m.

    `server/CLAUDE.md` already described this failure for health ("does so
    table-wide so one working phone masks another's dead one"); nothing enforced it.
    """

    def _users(self, db_session):
        from app.models.users import User

        return db_session.query(User).order_by(User.id).all()

    def test_a_lagging_owner_is_not_hidden_by_a_healthy_one(self, db_session):
        """The exact live scenario: one daemon fresh, one stale, one threshold."""
        from datetime import datetime, timedelta, timezone

        from app.services import data_freshness as df

        now = datetime.now(timezone.utc)
        users = self._users(db_session)
        assert len(users) >= 2, "fixture seeds two users"
        users[0].reminders_verified_at = now                      # healthy
        users[1].reminders_verified_at = now - timedelta(hours=10)  # silent (> 540m threshold)
        db_session.commit()

        results = [
            r for r in df.check_all(db_session) if r.integration == "apple_reminders"
        ]
        by_user = {r.user_id: r for r in results}

        assert set(by_user) == {users[0].id, users[1].id}, "expected one row per owner"
        assert by_user[users[0].id].age_seconds < 300
        stale = by_user[users[1].id]
        assert stale.age_seconds > stale.threshold_seconds, (
            "the lagging owner was not reported as over threshold"
        )

    def test_an_owner_with_no_data_is_not_reported_stale(self, db_session):
        """Per user, "never had data" almost always means "doesn't use this",
        and alerting on it is permanent noise. Contrast a table-wide probe, where
        an empty table genuinely is a broken pipeline."""
        from datetime import datetime, timezone

        from app.services import data_freshness as df

        users = self._users(db_session)
        users[0].reminders_verified_at = datetime.now(timezone.utc)
        users[1].reminders_verified_at = None
        db_session.commit()

        results = [
            r for r in df.check_all(db_session) if r.integration == "apple_reminders"
        ]
        assert {r.user_id for r in results} == {users[0].id}

    def test_the_alert_text_names_the_owner(self, db_session):
        """"reminders data is stale" is not actionable when two Macs feed it and
        only one has stopped."""
        import json
        from datetime import datetime, timedelta, timezone

        from app.integrations.system.tools import handle_alerts_household

        now = datetime.now(timezone.utc)
        users = self._users(db_session)
        users[0].reminders_verified_at = now
        users[1].reminders_verified_at = now - timedelta(hours=10)  # > 540m threshold
        db_session.commit()

        # The household view deliberately — that is what the notifications sweep
        # and the dashboard render from, and the only view that must show every
        # owner. The scoped view's filtering is asserted separately below.
        payload = json.loads(handle_alerts_household(db_session, {}))

        reminders = [a for a in payload.get("alerts", []) if a["integration"] == "apple_reminders"]
        assert reminders, "a 3-hour-silent daemon did not raise an alert at all"
        issues = " ".join(reminders[0]["issues"])
        label = users[1].display_name or users[1].name
        assert label in issues, f"alert did not name the affected owner: {issues!r}"

    def test_a_scoped_caller_does_not_see_another_owners_stall(self, db_session):
        """The flip side: Alex's alerts panel must not report Sam's dead daemon.

        Same rule this function already applies to health coverage gaps and OAuth
        re-auth — a row attributable to one user_id is filtered to that caller.
        """
        import json
        from datetime import datetime, timedelta, timezone

        from app.auth.context import use_user
        from app.integrations.system.tools import handle_alerts

        now = datetime.now(timezone.utc)
        users = self._users(db_session)
        users[0].reminders_verified_at = now                        # caller: fine
        users[1].reminders_verified_at = now - timedelta(hours=10)  # other: stalled (> 540m threshold)
        db_session.commit()

        other_label = users[1].display_name or users[1].name
        with use_user(users[0].id):
            payload = json.loads(handle_alerts(db_session, {}))

        issues = " ".join(
            i for a in payload.get("alerts", []) for i in a.get("issues", [])
        )
        assert other_label not in issues, (
            f"a scoped caller was shown another owner's stalled daemon: {issues!r}"
        )


@pytest.mark.db
class TestEffectiveThresholdMinutesIsConfigDriven:
    """2026-08-27: `apple_reminders`' staleness threshold used to be a bare
    `threshold_minutes=5` on the manifest. It's now overridable via
    `apple_reminders.reminders_stale_minutes` (`threshold_config_key`),
    falling back to the static manifest value on any config problem — see
    `_effective_threshold_minutes`."""

    def _users(self, db_session):
        from app.models.users import User

        return db_session.query(User).order_by(User.id).all()

    def test_config_override_lowers_the_threshold(self, db_session, monkeypatch):
        from types import SimpleNamespace

        now = datetime.now(timezone.utc)
        users = self._users(db_session)
        users[0].reminders_verified_at = now - timedelta(minutes=10)
        db_session.commit()

        # Default (540m) does not consider 10 minutes stale.
        results = [r for r in df.check_all(db_session) if r.integration == "apple_reminders"]
        stale = next(r for r in results if r.user_id == users[0].id)
        assert stale.age_seconds < stale.threshold_seconds

        # A tighter configured override does. `plugin_config` is imported
        # lazily inside `_effective_threshold_minutes`, so patch it where
        # it's actually looked up rather than on `df`.
        import app.plugin.config_store as config_store

        monkeypatch.setattr(
            config_store, "plugin_config", lambda name: SimpleNamespace(reminders_stale_minutes=5)
        )
        results = [r for r in df.check_all(db_session) if r.integration == "apple_reminders"]
        stale = next(r for r in results if r.user_id == users[0].id)
        assert stale.age_seconds > stale.threshold_seconds

    def test_bad_config_value_falls_back_to_the_static_threshold(self, db_session, monkeypatch):
        from types import SimpleNamespace

        import app.plugin.config_store as config_store

        now = datetime.now(timezone.utc)
        users = self._users(db_session)
        users[0].reminders_verified_at = now - timedelta(hours=10)  # exceeds 540m fallback
        db_session.commit()

        monkeypatch.setattr(
            config_store, "plugin_config",
            lambda name: SimpleNamespace(reminders_stale_minutes="not-a-number"),
        )
        results = [r for r in df.check_all(db_session) if r.integration == "apple_reminders"]
        stale = next(r for r in results if r.user_id == users[0].id)
        assert stale.threshold_seconds == 540 * 60
        assert stale.age_seconds > stale.threshold_seconds
