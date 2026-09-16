"""Tests for the `notifications` integration.

The interesting surface is not "does it POST" — it's the deduplication, because
the failure mode this package prevents is an alerting channel that gets muted
for crying wolf every 15 minutes. So the bulk of these tests pin the ledger
lifecycle and the fingerprint stability that makes it work.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.integrations.notifications import sweep
from app.integrations.notifications.models import NotificationSend


def _config(**overrides):
    """Config stand-in matching the manifest's schema defaults.

    The three push-gate keys (`min_active_minutes`, `refire_cooldown_minutes`,
    `quiet_hours`) deliberately default to *disabled* here (0 / 0 / "")
    rather than mirroring the manifest's sleep-tolerant production defaults
    (30 / 120 / "22:00-07:30") — nearly every existing test in this module
    predates the gate and asserts "a new alert pushes immediately", which is
    only true with the gate off. `TestPushGating` below turns each one on
    explicitly and pins its behaviour.
    """
    base = {
        "targets": {},
        "household_targets": ["mobile_app_household_phone"],
        "threshold_minutes": 60,
        "resend_after_minutes": 1440,
        "notify_on_recovery": True,
        "suppress_push_for_user_ids": [],
        "sleep_deadline_user_ids": [],
        "sleep_deadline_hour": 10,
        "sleep_deadline_timezone": "Europe/Dublin",
        "min_active_minutes": 0,
        "refire_cooldown_minutes": 0,
        "quiet_hours": "",
        "quiet_hours_timezone": "Europe/Dublin",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _RecordingClient:
    """Stands in for `client`, recording publishes instead of making them."""

    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str, str]] = []
        # (title, user_id) per publish — `sent` predates per-user routing and
        # deliberately keeps its 3-tuple shape so existing assertions hold.
        self.routed: list[tuple[str, int | None]] = []
        self.fail = fail

    def publish(self, title, body, severity="warning", user_id=None, **kwargs):
        # `**kwargs` swallows `source`/`fingerprint`/`ledger` — sweep.py's
        # calls pass these (`source="sweep", ledger=False`) so the ledger
        # write for a sweep-originated send stays sweep's own job (see
        # `client.py::publish`'s "Ledger every publish" note); this double
        # doesn't need to record them to stay a faithful stand-in.
        if self.fail:
            from app.errors import TransientError

            raise TransientError("HA notify unreachable")
        self.sent.append((title, body, severity))
        self.routed.append((title, user_id))

    def current_topic(self):
        return "test-topic"


@pytest.fixture
def stub(monkeypatch):
    """Wire sweep's two external dependencies to test doubles."""
    recording = _RecordingClient()
    monkeypatch.setattr(sweep, "client", recording)
    monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
    return recording


# ---------------------------------------------------------------------------
# Fingerprint stability — the core invariant
# ---------------------------------------------------------------------------

class TestIssueKind:
    @pytest.mark.parametrize(
        "issue,expected",
        [
            ("failing (3x consecutive)", "failing"),
            ("failing (21x consecutive)", "failing"),
            ("sync stale (last sync 3h 12m ago)", "sync_stale"),
            ("sync stale (last sync 47m ago)", "sync_stale"),
            ("data stale (no records in table)", "data_stale"),
            ("data stale (latest record 5h old, threshold 90m)", "data_stale"),
            ("never synced", "never_synced"),
            ("failing repeatedly (7x errors in the last 60m)", "tool_failing"),
            ("p95 duration 8123ms over last 24h (150 calls)", "tool_slow"),
        ],
    )
    def test_known_issues_map_to_stable_kinds(self, issue, expected):
        assert sweep._issue_kind(issue) == expected

    def test_same_problem_different_age_gives_one_fingerprint(self):
        """The regression this whole design exists for.

        `system_alerts` re-renders ages every sweep. If the fingerprint tracked
        the rendered text, every sweep would look like a brand-new alert and the
        ledger would suppress nothing.
        """
        first = sweep._issue_kind("sync stale (last sync 1h 5m ago)")
        later = sweep._issue_kind("sync stale (last sync 9h 41m ago)")
        assert first == later

    def test_failing_repeatedly_is_not_mistaken_for_failing(self):
        """Order-dependence in `_ISSUE_KINDS` — a slow/broken *tool* alert must
        not be filed under the *sync* failure kind."""
        assert sweep._issue_kind("failing repeatedly (5x errors)") == "tool_failing"
        assert sweep._issue_kind("failing (5x consecutive)") == "failing"

    def test_unknown_issue_still_yields_something_stable(self):
        """Degrade, never raise — a crashing sweep notifies about nothing."""
        a = sweep._issue_kind("brand new problem shape (12 things)")
        b = sweep._issue_kind("brand new problem shape (99 things)")
        assert a == b
        assert a

    def test_prefixes_still_match_the_real_alerts_handler(self):
        """Guard the coupling declared in `_ISSUE_KINDS`' comment.

        These prefixes mirror strings built in `system/tools.py`. If that
        handler's wording changes, dedup silently degrades to slug-matching —
        so assert each prefix is still present in that module's source.
        """
        from pathlib import Path

        import app.integrations.system.tools as system_tools

        source = Path(system_tools.__file__).read_text()
        for prefix, _kind in sweep._ISSUE_KINDS:
            assert prefix in source, f"'{prefix}' no longer appears in system/tools.py"


# ---------------------------------------------------------------------------
# collect() — flattening the payload
# ---------------------------------------------------------------------------

class TestCollect:
    def _payload(self, monkeypatch, payload):
        monkeypatch.setattr(
            sweep,
            "get_capability",
            lambda name: SimpleNamespace(alerts_household=lambda s, a: json.dumps(payload)),
        )

    def test_all_ok_collects_nothing(self, monkeypatch, mock_session):
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        self._payload(monkeypatch, {"status": "all_ok", "alerts": [], "reauth_needed": [], "tool_alerts": []})
        assert sweep.collect(mock_session) == []

    def test_two_issues_on_one_integration_are_two_items(self, monkeypatch, mock_session):
        """They can resolve independently, so collapsing them would let a
        lingering problem hide behind a fixed one."""
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        self._payload(monkeypatch, {
            "alerts": [{
                "integration": "obsidian",
                "consecutive_failures": 1,
                "issues": ["failing (1x consecutive)", "data stale (no records in table)"],
            }],
        })
        items = sweep.collect(mock_session)
        assert {i.fingerprint for i in items} == {
            "integration:obsidian:failing",
            "integration:obsidian:data_stale",
        }

    def test_many_consecutive_failures_escalate_to_critical(self, monkeypatch, mock_session):
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        self._payload(monkeypatch, {
            "alerts": [{
                "integration": "lastfm",
                "consecutive_failures": 9,
                "issues": ["failing (9x consecutive)"],
            }],
        })
        assert sweep.collect(mock_session)[0].severity == "critical"

    def test_reauth_is_always_critical_and_carries_the_link(self, monkeypatch, mock_session):
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        self._payload(monkeypatch, {
            "reauth_needed": [{
                "provider": "google",
                "account_email": "someone@example.com",
                "reason": "refresh token revoked",
                "reauth_url": "/api/auth/google/login?account=someone@example.com",
            }],
        })
        item = sweep.collect(mock_session)[0]
        assert item.severity == "critical"
        assert item.fingerprint == "reauth:google:someone@example.com"
        assert "/api/auth/google/login" in item.body


# ---------------------------------------------------------------------------
# reconcile() — the ledger lifecycle (real Postgres: partial unique index)
# ---------------------------------------------------------------------------

@pytest.mark.db
class TestReconcile:
    def _item(self, fingerprint="integration:obsidian:data_stale", body="obsidian: data stale"):
        return sweep.AlertItem(
            fingerprint=fingerprint,
            title="lios: obsidian degraded",
            body=body,
            severity="warning",
        )

    def test_new_alert_sends_once_and_opens_a_row(self, db_session, stub):
        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["new"] == 1
        assert len(stub.sent) == 1
        row = db_session.query(NotificationSend).one()
        assert row.resolved_at is None
        assert row.send_count == 1

    def test_same_alert_next_sweep_is_suppressed(self, db_session, stub):
        sweep.reconcile(db_session, [self._item()])
        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["suppressed"] == 1
        assert counts["new"] == 0
        assert len(stub.sent) == 1, "second sweep must not re-notify"
        assert db_session.query(NotificationSend).count() == 1

    def test_changing_age_in_body_does_not_re_notify(self, db_session, stub):
        """End-to-end version of the fingerprint-stability test above."""
        sweep.reconcile(db_session, [self._item(body="obsidian: data stale (2h old)")])
        sweep.reconcile(db_session, [self._item(body="obsidian: data stale (9h old)")])

        assert len(stub.sent) == 1
        # The ledger still tracks the *current* text.
        assert "9h old" in db_session.query(NotificationSend).one().body

    def test_resend_once_the_window_elapses(self, db_session, stub):
        sweep.reconcile(db_session, [self._item()])
        row = db_session.query(NotificationSend).one()
        row.last_sent_at = datetime.now(timezone.utc) - timedelta(days=2)
        db_session.commit()

        counts = sweep.reconcile(db_session, [self._item()])
        assert counts["resent"] == 1
        assert len(stub.sent) == 2
        assert db_session.query(NotificationSend).one().send_count == 2

    def test_cleared_alert_resolves_and_announces_recovery(self, db_session, stub):
        sweep.reconcile(db_session, [self._item()])
        counts = sweep.reconcile(db_session, [])

        assert counts["resolved"] == 1
        row = db_session.query(NotificationSend).one()
        assert row.resolved_at is not None
        assert stub.sent[-1][2] == "recovery"

    def test_recurrence_after_resolution_opens_a_second_row(self, db_session, stub):
        """History is kept per episode — and the partial unique index has to
        permit that, which a plain unique constraint would not."""
        sweep.reconcile(db_session, [self._item()])
        sweep.reconcile(db_session, [])
        sweep.reconcile(db_session, [self._item()])

        rows = db_session.query(NotificationSend).all()
        assert len(rows) == 2
        assert sum(1 for r in rows if r.resolved_at is None) == 1

    def test_failed_publish_leaves_the_row_open_for_retry(self, db_session, monkeypatch):
        """A send that never landed must not be recorded as delivered."""
        failing = _RecordingClient(fail=True)
        monkeypatch.setattr(sweep, "client", failing)
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())

        sweep.reconcile(db_session, [self._item()])
        row = db_session.query(NotificationSend).one()
        assert row.send_count == 0
        assert row.last_sent_at is None

        # Next sweep retries rather than suppressing.
        working = _RecordingClient()
        monkeypatch.setattr(sweep, "client", working)
        counts = sweep.reconcile(db_session, [self._item()])
        assert counts["resent"] == 1
        assert len(working.sent) == 1

    def test_no_recovery_ping_for_an_alert_that_never_sent(self, db_session, monkeypatch):
        """Nothing to un-say if the phone was never told in the first place."""
        failing = _RecordingClient(fail=True)
        monkeypatch.setattr(sweep, "client", failing)
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        sweep.reconcile(db_session, [self._item()])

        working = _RecordingClient()
        monkeypatch.setattr(sweep, "client", working)
        sweep.reconcile(db_session, [])

        assert working.sent == []
        assert db_session.query(NotificationSend).one().resolved_at is not None

    def test_recovery_can_be_switched_off(self, db_session, monkeypatch):
        recording = _RecordingClient()
        monkeypatch.setattr(sweep, "client", recording)
        monkeypatch.setattr(
            sweep, "plugin_config", lambda name: _config(notify_on_recovery=False)
        )
        sweep.reconcile(db_session, [self._item()])
        sweep.reconcile(db_session, [])

        assert len(recording.sent) == 1
        assert all(s[2] != "recovery" for s in recording.sent)


# ---------------------------------------------------------------------------
# Config enforcement at the call site (manifest deliberately not `required`)
# ---------------------------------------------------------------------------

class _FakeNotifyFacade:
    """Stands in for `get_capability("homeassistant.notify")`."""

    def __init__(self, fail_targets: set[str] | None = None, raise_cls=None):
        self.calls: list[tuple[str, str, str, dict]] = []
        self.fail_targets = fail_targets or set()
        self.raise_cls = raise_cls

    def notify(self, target, title, message, data=None):
        if target in self.fail_targets:
            from app.errors import TransientError

            raise (self.raise_cls or TransientError)(f"{target} unreachable")
        self.calls.append((target, title, message, data or {}))
        return True


class TestConfigEnforcement:
    def test_missing_household_targets_raises_a_permanent_error_naming_it(self, monkeypatch):
        from app.errors import PermanentError
        from app.integrations.notifications import client as ha_client

        monkeypatch.setattr(
            ha_client, "plugin_config", lambda name: _config(household_targets=[])
        )

        with pytest.raises(PermanentError) as excinfo:
            ha_client.publish("t", "b")

        assert "household_targets" in str(excinfo.value)

    def test_missing_per_user_target_raises_a_permanent_error_naming_the_user(self, monkeypatch):
        from app.errors import PermanentError
        from app.integrations.notifications import client as ha_client

        monkeypatch.setattr(
            ha_client, "plugin_config", lambda name: _config(targets={})
        )

        with pytest.raises(PermanentError) as excinfo:
            ha_client.publish("t", "b", user_id=1)

        message = str(excinfo.value)
        assert "1" in message
        assert "targets" in message

    def test_send_tool_returns_the_config_error_as_data(self, monkeypatch, mock_session):
        """A tool should hand back an actionable message, not a stack trace.

        The caller (user 1 here) has no `targets` entry, so the error names
        that user's missing mapping — since 2026-09-06 `notify_send` routes
        to the caller, not household-wide."""
        from app.integrations.notifications import client as ha_client
        from app.integrations.notifications import tools

        monkeypatch.setattr(
            ha_client, "plugin_config", lambda name: _config(targets={})
        )
        result = json.loads(tools.handle_send(mock_session, {"message": "hello"}))

        assert result["sent"] is False
        assert "targets" in result["error"]
        assert '"1"' in result["error"]


# ---------------------------------------------------------------------------
# publish() — HA routing, fan-out, and severity mapping
# ---------------------------------------------------------------------------

class TestPublishRouting:
    def _wire(self, monkeypatch, facade, **config_overrides):
        from app.integrations.notifications import client as ha_client

        monkeypatch.setattr(ha_client, "plugin_config", lambda name: _config(**config_overrides))
        monkeypatch.setattr(ha_client, "get_capability", lambda name: facade)
        # These tests are about HA routing/fan-out/severity, not the ledger —
        # `_record_send` (the "ledger every publish" write, see client.py)
        # needs a real DB, which this unit-tier class has none of. The
        # ledger-write behaviour itself is covered by
        # `TestAdHocPublishLedgersEverySend` below (`@pytest.mark.db`).
        monkeypatch.setattr(ha_client, "_record_send", lambda *a, **k: None)
        return ha_client

    def test_household_send_fans_out_to_every_configured_target(self, monkeypatch):
        facade = _FakeNotifyFacade()
        ha_client = self._wire(
            monkeypatch, facade,
            household_targets=["mobile_app_a", "mobile_app_b"],
        )

        ha_client.publish("lios: obsidian degraded", "obsidian: data stale", "warning")

        targets = {call[0] for call in facade.calls}
        assert targets == {"mobile_app_a", "mobile_app_b"}

    def test_per_user_send_routes_to_only_that_users_target(self, monkeypatch):
        facade = _FakeNotifyFacade()
        ha_client = self._wire(
            monkeypatch, facade,
            targets={"1": "mobile_app_a", "2": "mobile_app_b"},
            household_targets=["mobile_app_household"],
        )

        ha_client.publish("t", "b", "warning", user_id=2)

        assert [call[0] for call in facade.calls] == ["mobile_app_b"]

    def test_one_failing_target_does_not_stop_the_rest(self, monkeypatch):
        facade = _FakeNotifyFacade(fail_targets={"mobile_app_a"})
        ha_client = self._wire(
            monkeypatch, facade,
            household_targets=["mobile_app_a", "mobile_app_b"],
        )

        # Must not raise — at least one target got the message.
        ha_client.publish("t", "b", "warning")

        assert [call[0] for call in facade.calls] == ["mobile_app_b"]

    def test_every_target_failing_raises(self, monkeypatch):
        from app.errors import TransientError

        facade = _FakeNotifyFacade(fail_targets={"mobile_app_a"})
        ha_client = self._wire(monkeypatch, facade, household_targets=["mobile_app_a"])

        with pytest.raises(TransientError):
            ha_client.publish("t", "b", "warning")

    @pytest.mark.parametrize(
        "severity,expected",
        [
            ("critical", {"push": {"interruption-level": "critical"}, "ttl": 0, "priority": "high"}),
            ("warning", {}),
            ("recovery", {"push": {"interruption-level": "passive"}, "importance": "low"}),
        ],
    )
    def test_severity_maps_to_the_documented_ha_payload(self, monkeypatch, severity, expected):
        facade = _FakeNotifyFacade()
        ha_client = self._wire(monkeypatch, facade, household_targets=["mobile_app_a"])

        ha_client.publish("t", "b", severity)

        assert facade.calls[0][3] == expected


class TestFacadeNeverRaises:
    def test_send_returns_false_when_the_sink_fails(self, monkeypatch):
        from app.integrations.notifications.facade import FACADE
        from app.integrations.notifications import client as ha_client
        from app.errors import TransientError

        def _raise(*args, **kwargs):
            raise TransientError("HA unreachable")

        monkeypatch.setattr(ha_client, "publish", _raise)

        assert FACADE.send("t", "b") is False

    def test_send_returns_false_on_an_unexpected_exception(self, monkeypatch):
        """Even a bug in the sink must not escape the facade."""
        from app.integrations.notifications.facade import FACADE
        from app.integrations.notifications import client as ha_client

        def _boom(*args, **kwargs):
            raise RuntimeError("bug")

        monkeypatch.setattr(ha_client, "publish", _boom)

        assert FACADE.send("t", "b") is False

    def test_send_passes_user_id_through(self, monkeypatch):
        from app.integrations.notifications.facade import FACADE
        from app.integrations.notifications import client as ha_client

        captured = {}

        def _record(title, body, severity="warning", user_id=None, **kwargs):
            captured["user_id"] = user_id
            return None

        monkeypatch.setattr(ha_client, "publish", _record)

        assert FACADE.send("t", "b", user_id=2) is True
        assert captured["user_id"] == 2


# ---------------------------------------------------------------------------
# F7 — notification_sends.user_id: attribution + scoped reads
# ---------------------------------------------------------------------------

class TestCollectUserAttribution:
    """`collect()` must recognise the one issue shape system/tools.py's health
    coverage axis produces ("data gap for user N: ...") and stamp
    `target_user_id`, with a fingerprint that keeps different users' gaps
    from colliding onto the same ledger row."""

    def _payload(self, monkeypatch, payload):
        monkeypatch.setattr(
            sweep,
            "get_capability",
            lambda name: SimpleNamespace(alerts_household=lambda s, a: json.dumps(payload)),
        )

    def test_health_gap_issue_carries_the_named_user_id(self, monkeypatch, mock_session):
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        self._payload(monkeypatch, {
            "alerts": [{
                "integration": "apple_health",
                "consecutive_failures": 0,
                "issues": ["data gap for user 2: 3 of last 7 days missing (2026-08-01)"],
            }],
        })
        item = sweep.collect(mock_session)[0]
        assert item.target_user_id == 2

    def test_ordinary_issue_has_no_target_user(self, monkeypatch, mock_session):
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        self._payload(monkeypatch, {
            "alerts": [{
                "integration": "obsidian",
                "consecutive_failures": 1,
                "issues": ["failing (1x consecutive)"],
            }],
        })
        item = sweep.collect(mock_session)[0]
        assert item.target_user_id is None

    def test_two_users_gaps_get_distinct_fingerprints(self, monkeypatch, mock_session):
        """Without the `:userN` fingerprint suffix, both gaps reduce to the
        same word-slug kind and collide onto one open ledger row — the second
        user's gap would silently overwrite the first's attribution."""
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        self._payload(monkeypatch, {
            "alerts": [{
                "integration": "apple_health",
                "consecutive_failures": 0,
                "issues": [
                    "data gap for user 1: 2 of last 7 days missing (2026-08-01)",
                    "data gap for user 2: 3 of last 7 days missing (2026-08-02)",
                ],
            }],
        })
        items = sweep.collect(mock_session)
        fingerprints = {i.fingerprint for i in items}
        assert len(fingerprints) == 2
        by_user = {i.target_user_id: i.fingerprint for i in items}
        assert by_user[1] != by_user[2]


@pytest.mark.db
class TestReconcileStampsUserId:
    def _item(self, user_id=None, fingerprint="integration:apple_health:data_gap:user2"):
        return sweep.AlertItem(
            fingerprint=fingerprint,
            title="lios: apple_health degraded",
            body="apple_health: data gap for user 2: 1 of last 7 days missing (2026-08-01)",
            severity="warning",
            target_user_id=user_id,
        )

    def test_new_row_gets_the_target_user_id(self, db_session, stub):
        sweep.reconcile(db_session, [self._item(user_id=2)])
        row = db_session.query(NotificationSend).one()
        assert row.user_id == 2

    def test_household_alert_leaves_user_id_null(self, db_session, stub):
        sweep.reconcile(db_session, [
            sweep.AlertItem(
                fingerprint="integration:obsidian:data_stale",
                title="lios: obsidian degraded",
                body="obsidian: data stale (no records)",
                severity="warning",
            )
        ])
        row = db_session.query(NotificationSend).one()
        assert row.user_id is None


@pytest.mark.db
class TestNotifyRecentScoping:
    """`notify_recent` must not let one user read another user's
    attributed body text — the actual F7 leak."""

    def _seed(self, db_session):
        from app.integrations.notifications import tools

        shared = NotificationSend(
            fingerprint="integration:obsidian:data_stale",
            title="lios: obsidian degraded",
            body="obsidian: data stale (no records)",
            severity="warning",
            topic="test-topic",
            user_id=None,
        )
        user1_row = NotificationSend(
            fingerprint="integration:apple_health:data_gap:user1",
            title="lios: apple_health degraded",
            body="apple_health: data gap for user 1: 2 days missing",
            severity="warning",
            topic="test-topic",
            user_id=1,
        )
        user2_row = NotificationSend(
            fingerprint="integration:apple_health:data_gap:user2",
            title="lios: apple_health degraded",
            body="apple_health: data gap for user 2: 3 days missing",
            severity="warning",
            topic="test-topic",
            user_id=2,
        )
        db_session.add_all([shared, user1_row, user2_row])
        db_session.commit()
        return tools

    def test_bound_user_sees_shared_plus_own_never_the_others(self, db_session):
        from app.auth.context import use_user

        tools = self._seed(db_session)
        with use_user(1):
            result = json.loads(tools.handle_recent(db_session, {"limit": 50}))
        bodies = " ".join(n["body"] for n in result["notifications"])
        assert "data gap for user 1" in bodies
        assert "data stale" in bodies
        assert "data gap for user 2" not in bodies

    def test_other_bound_user_sees_their_own_not_the_first(self, db_session):
        from app.auth.context import use_user

        tools = self._seed(db_session)
        with use_user(2):
            result = json.loads(tools.handle_recent(db_session, {"limit": 50}))
        bodies = " ".join(n["body"] for n in result["notifications"])
        assert "data gap for user 2" in bodies
        assert "data gap for user 1" not in bodies

    def test_unbound_caller_sees_only_household_shared_rows(self, db_session):
        """Per `auth/context.py`'s documented stance: unbound means
        household-shared only, never everyone's private data."""
        from app.auth.context import _current_user_id

        tools = self._seed(db_session)
        token = _current_user_id.set(0)
        try:
            result = json.loads(tools.handle_recent(db_session, {"limit": 50}))
        finally:
            _current_user_id.reset(token)
        bodies = " ".join(n["body"] for n in result["notifications"])
        assert "data stale" in bodies
        assert "data gap for user 1" not in bodies
        assert "data gap for user 2" not in bodies


class TestManifestWiring:
    def test_declares_notify_push_and_depends_on_system_alerts_and_ha_notify(self):
        from app.integrations.notifications.manifest import MANIFEST

        # notify.email joined notify.push on this manifest 2026-08-29 — same
        # integration, a second capability method on the same facade (see
        # facade.py's docstring).
        assert MANIFEST.provides == ["notify.push", "notify.email"]
        assert "system.alerts" in MANIFEST.depends_on
        assert "homeassistant.notify" in MANIFEST.depends_on

    def test_system_provides_the_alerts_capability(self):
        """`depends_on` above only validates if `system` actually offers it."""
        from app.integrations.system.manifest import MANIFEST as SYSTEM

        assert "system.alerts" in SYSTEM.provides

    def test_homeassistant_provides_the_notify_capability(self):
        """`depends_on` above only validates if `homeassistant` actually offers it."""
        from app.integrations.homeassistant.manifest import MANIFEST as HA

        assert "homeassistant.notify" in HA.provides

    def test_sweep_is_a_cron_background_task_not_a_sync_schedule(self):
        """Deliberate: cron tasks run even when unconfigured (scheduler.py:217),
        so the sweep stays live and reports its own misconfiguration."""
        from app.integrations.notifications.manifest import MANIFEST

        assert MANIFEST.schedule is None
        task = next(t for t in MANIFEST.background_tasks if t.name == "notifications_alert_sweep")
        assert task.kind == "cron"
        assert task.cron

    def test_no_staleness_probe(self):
        """A quiet ledger is the healthy state — a probe would make the
        alerting system alert about its own silence."""
        from app.integrations.notifications.manifest import MANIFEST

        assert MANIFEST.staleness_probe is None

    def test_config_defaults_carry_no_deployment_literals(self):
        """Belt-and-braces alongside test_personalisation_guard.py: a real
        notify-service target names a device and must never gain a default."""
        from app.integrations.notifications.manifest import MANIFEST

        assert MANIFEST.config_schema["targets"].default == {}
        assert MANIFEST.config_schema["household_targets"].default == []


class TestStructuralAttribution:
    """`issue_users` (set by `system/tools.py::_attribute`) must win over the
    legacy prose regex, and must work for the shapes the regex never covered —
    per-owner staleness rows and daemon liveness. The regex only ever matched
    "data gap for user N:", so before this an alert reading "data stale for
    Sam" was household-shared as far as the sweep could tell.
    """

    def _payload(self, monkeypatch, payload):
        monkeypatch.setattr(
            sweep,
            "get_capability",
            lambda name: SimpleNamespace(alerts_household=lambda s, a: json.dumps(payload)),
        )

    def test_per_owner_staleness_row_is_attributed(self, monkeypatch, mock_session):
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        issue = "data stale for Sam (latest record 1h 2m old, threshold 5m)"
        self._payload(monkeypatch, {
            "alerts": [{
                "integration": "apple_reminders",
                "consecutive_failures": 0,
                "issues": [issue],
                "issue_users": {issue: 2},
            }],
        })
        item = sweep.collect(mock_session)[0]
        assert item.target_user_id == 2
        assert item.fingerprint.endswith(":user2")

    def test_two_owners_on_one_integration_are_attributed_separately(
        self, monkeypatch, mock_session
    ):
        """The case a per-*entry* user_id could not express: a `per_user` probe
        emits one row per owner and both land under the same integration name,
        so attribution has to be per issue."""
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        alex = "data stale for Alex (latest record 2h old, threshold 5m)"
        sam = "data stale for Sam (latest record 3h old, threshold 5m)"
        self._payload(monkeypatch, {
            "alerts": [{
                "integration": "apple_reminders",
                "consecutive_failures": 0,
                "issues": [alex, sam],
                "issue_users": {alex: 1, sam: 2},
            }],
        })
        items = sweep.collect(mock_session)
        assert {i.target_user_id for i in items} == {1, 2}
        assert len({i.fingerprint for i in items}) == 2

    def test_entry_level_user_id_attributes_a_daemon_alert(self, monkeypatch, mock_session):
        """A daemon entry is keyed on a `client_tokens` label owned by exactly
        one person, so `user_id` on the entry is enough."""
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        self._payload(monkeypatch, {
            "alerts": [{
                "integration": "sam-macbook",
                "consecutive_failures": 0,
                "issues": ["daemon silent (last seen 1h 2m ago)"],
                "user_id": 2,
            }],
        })
        item = sweep.collect(mock_session)[0]
        assert item.target_user_id == 2

    def test_regex_still_works_without_structural_data(self, monkeypatch, mock_session):
        """The fallback must survive: a payload produced before `issue_users`
        existed still attributes rather than reverting to household-wide."""
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        self._payload(monkeypatch, {
            "alerts": [{
                "integration": "apple_health",
                "consecutive_failures": 0,
                "issues": ["data gap for user 2: 3 of last 7 days missing (2026-08-01)"],
            }],
        })
        assert sweep.collect(mock_session)[0].target_user_id == 2


class TestPushSuppression:
    """A suppressed user's attributable alerts must not be pushed — but must
    still be *detected*, which is why suppression sits at the push boundary and
    not in `check_all`."""

    def _payload(self, monkeypatch, payload):
        monkeypatch.setattr(
            sweep,
            "get_capability",
            lambda name: SimpleNamespace(alerts_household=lambda s, a: json.dumps(payload)),
        )

    def _two_owners(self, monkeypatch, suppress):
        monkeypatch.setattr(
            sweep, "plugin_config",
            lambda name: _config(suppress_push_for_user_ids=suppress),
        )
        alex = "data stale for Alex (latest record 2h old, threshold 5m)"
        sam = "data stale for Sam (latest record 3h old, threshold 5m)"
        self._payload(monkeypatch, {
            "alerts": [
                {
                    "integration": "apple_reminders",
                    "consecutive_failures": 0,
                    "issues": [alex, sam],
                    "issue_users": {alex: 1, sam: 2},
                },
                {
                    "integration": "google_mail",
                    "consecutive_failures": 2,
                    "issues": ["failing (2x consecutive)"],
                },
            ],
        })

    def test_suppressed_user_is_dropped(self, monkeypatch, mock_session):
        self._two_owners(monkeypatch, ["2"])
        owners = [i.target_user_id for i in sweep.collect(mock_session)]
        assert 2 not in owners
        assert 1 in owners

    def test_household_alerts_are_never_suppressed(self, monkeypatch, mock_session):
        """Suppression is scoped to one person's own devices. A failing Gmail
        sync has no owner and must keep notifying regardless."""
        self._two_owners(monkeypatch, ["1", "2"])
        items = sweep.collect(mock_session)
        assert [i.target_user_id for i in items] == [None]
        assert "google_mail" in items[0].body

    def test_empty_config_suppresses_nothing(self, monkeypatch, mock_session):
        self._two_owners(monkeypatch, [])
        assert {i.target_user_id for i in sweep.collect(mock_session)} == {1, 2, None}

    def test_junk_config_entry_does_not_silence_everything(self, monkeypatch, mock_session):
        """A mistyped config value must cost one entry, not the whole channel —
        the failure mode of an alerting system is silence."""
        self._two_owners(monkeypatch, ["not-a-number", "2"])
        owners = {i.target_user_id for i in sweep.collect(mock_session)}
        assert owners == {1, None}


class TestPerUserRouting:
    """`_publish` must route an attributed item to that person's device. It was
    always household-wide before, which for the sleep deadline would mean
    telling the wrong person their sleep data is missing."""

    def test_attributed_item_routes_to_its_owner(self, db_session, stub, monkeypatch):
        item = sweep.AlertItem(
            fingerprint="deadline:sleep:1:2026-08-19",
            title="lios: no sleep data for last night",
            body="missing",
            severity="warning",
            target_user_id=1,
        )
        sweep.reconcile(db_session, [item])
        assert stub.routed == [("lios: no sleep data for last night", 1)]

    def test_household_item_routes_household_wide(self, db_session, stub, monkeypatch):
        item = sweep.AlertItem(
            fingerprint="integration:google_mail:failing",
            title="lios: google_mail degraded",
            body="failing",
            severity="warning",
        )
        sweep.reconcile(db_session, [item])
        assert stub.routed == [("lios: google_mail degraded", None)]

    def test_missing_per_user_target_falls_back_to_household(self, db_session, monkeypatch):
        """A misrouted alert beats a dropped one — but the fallback only fires
        for a genuine config gap, not for a transport failure."""
        calls: list[int | None] = []

        class _Client:
            NotifyConfigError = sweep.NotifyConfigError

            def publish(self, title, body, severity="warning", user_id=None, **kwargs):
                calls.append(user_id)
                if user_id is not None:
                    raise sweep.NotifyConfigError("no target for user")

            def current_topic(self):
                return "test-topic"

        monkeypatch.setattr(sweep, "client", _Client())
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        row = sweep.NotificationSend(
            fingerprint="deadline:sleep:1:2026-08-19",
            title="t", body="b", severity="warning", topic="x", send_count=0,
        )
        db_session.add(row)
        item = sweep.AlertItem("deadline:sleep:1:2026-08-19", "t", "b", "warning", 1)
        sweep._publish(row, item, datetime.now(timezone.utc))
        assert calls == [1, None]
        assert row.send_count == 1


# ---------------------------------------------------------------------------
# Push-boundary gating (2026-08-27) — the flap fix.
#
# Detection is untouched by any of this: `collect()` and the axes in
# `system/tools.py` keep firing exactly as before. These tests are entirely
# about whether a detected, already-ledgered alert is *allowed to push* —
# see `sweep.py`'s module docstring and `_push_gate`.
# ---------------------------------------------------------------------------

def _quiet_window(start_offset_minutes: int, end_offset_minutes: int) -> str:
    """Build a `"HH:MM-HH:MM"` quiet-hours string bracketing *now*, in
    `Europe/Dublin` (the manifest default), by shifting the current local
    datetime rather than hand-computing hour/minute arithmetic — shifting the
    full datetime (not just its `.time()`) is what makes this correctly wrap
    across midnight for a window like (-5, +5) computed at 00:02.
    """
    now_local = datetime.now(ZoneInfo("Europe/Dublin"))
    start = (now_local + timedelta(minutes=start_offset_minutes)).time()
    end = (now_local + timedelta(minutes=end_offset_minutes)).time()
    return f"{start:%H:%M}-{end:%H:%M}"


@pytest.mark.db
class TestPersistenceGate:
    def _item(self, fingerprint="integration:x:gate"):
        return sweep.AlertItem(
            fingerprint=fingerprint, title="t", body="b", severity="warning",
        )

    def _wire(self, monkeypatch, **overrides):
        recording = _RecordingClient()
        monkeypatch.setattr(sweep, "client", recording)
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config(**overrides))
        return recording

    def test_new_alert_does_not_push_before_the_gate_elapses(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, min_active_minutes=30)

        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["held"] == 1
        assert counts["new"] == 0
        assert recording.sent == []
        row = db_session.query(NotificationSend).one()
        assert row.resolved_at is None
        assert row.last_sent_at is None
        assert row.suppressed_reason == "min_active_gate"

    def test_pushes_once_continuously_active_long_enough(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, min_active_minutes=30)
        sweep.reconcile(db_session, [self._item()])
        row = db_session.query(NotificationSend).one()
        row.first_seen_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        db_session.commit()

        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["resent"] == 1
        assert len(recording.sent) == 1
        row = db_session.query(NotificationSend).one()
        assert row.suppressed_reason is None
        assert row.send_count == 1

    def test_resolving_before_the_gate_elapses_never_pushes(self, db_session, monkeypatch):
        """The regression this whole mechanism exists for: a MacBook asleep
        for 15-45 minutes must never reach a phone."""
        recording = self._wire(monkeypatch, min_active_minutes=30)
        sweep.reconcile(db_session, [self._item()])  # held, first sighting

        counts = sweep.reconcile(db_session, [])  # gone before the gate elapsed

        assert counts["resolved"] == 1
        assert recording.sent == [], "nothing was ever sent, so nothing should be un-said"
        row = db_session.query(NotificationSend).one()
        assert row.resolved_at is not None
        assert row.send_count == 0

    def test_zero_disables_the_gate(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, min_active_minutes=0)

        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["new"] == 1
        assert len(recording.sent) == 1


@pytest.mark.db
class TestRefireCooldown:
    FINGERPRINT = "integration:x:cooldown"

    def _item(self):
        return sweep.AlertItem(
            fingerprint=self.FINGERPRINT, title="t", body="b", severity="warning",
        )

    def _wire(self, monkeypatch, **overrides):
        recording = _RecordingClient()
        monkeypatch.setattr(sweep, "client", recording)
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config(**overrides))
        return recording

    @staticmethod
    def _non_recovery_sends(recording):
        """`_config()`'s `notify_on_recovery=True` means every resolve in
        these tests also pushes a recovery message — filter that out so the
        assertions are about the refire, not an unrelated push."""
        return [s for s in recording.sent if s[2] != "recovery"]

    def test_refiring_within_the_cooldown_does_not_push(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, refire_cooldown_minutes=120)
        sweep.reconcile(db_session, [self._item()])  # first episode: sends
        assert len(self._non_recovery_sends(recording)) == 1
        sweep.reconcile(db_session, [])  # resolves immediately after

        counts = sweep.reconcile(db_session, [self._item()])  # refires right away

        assert counts["held"] == 1
        assert len(self._non_recovery_sends(recording)) == 1, "the refire must not have pushed"
        rows = db_session.query(NotificationSend).order_by(NotificationSend.id).all()
        assert len(rows) == 2, "a new episode still opens its own row"
        assert rows[-1].suppressed_reason == "refire_cooldown"

    def test_refiring_after_the_cooldown_elapses_pushes(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, refire_cooldown_minutes=120)
        sweep.reconcile(db_session, [self._item()])
        sweep.reconcile(db_session, [])
        resolved_row = (
            db_session.query(NotificationSend)
            .filter(NotificationSend.resolved_at.isnot(None))
            .one()
        )
        resolved_row.resolved_at = datetime.now(timezone.utc) - timedelta(minutes=121)
        db_session.commit()

        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["new"] == 1
        assert len(self._non_recovery_sends(recording)) == 2
        new_row = (
            db_session.query(NotificationSend)
            .filter(NotificationSend.resolved_at.is_(None))
            .one()
        )
        assert new_row.suppressed_reason is None

    def test_zero_disables_the_cooldown(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, refire_cooldown_minutes=0)
        sweep.reconcile(db_session, [self._item()])
        sweep.reconcile(db_session, [])

        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["new"] == 1
        assert len(self._non_recovery_sends(recording)) == 2


@pytest.mark.db
class TestQuietHours:
    def _item(self, fingerprint="integration:x:quiet"):
        return sweep.AlertItem(
            fingerprint=fingerprint, title="t", body="b", severity="warning",
        )

    def _wire(self, monkeypatch, **overrides):
        recording = _RecordingClient()
        monkeypatch.setattr(sweep, "client", recording)
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config(**overrides))
        return recording

    def test_non_critical_push_is_held_during_quiet_hours(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, quiet_hours=_quiet_window(-5, 5))

        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["held"] == 1
        assert recording.sent == []
        row = db_session.query(NotificationSend).one()
        assert row.suppressed_reason == "quiet_hours"

    def test_delivered_once_at_window_end_if_still_active(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, quiet_hours=_quiet_window(-5, 5))
        sweep.reconcile(db_session, [self._item()])
        assert recording.sent == []

        # Window has ended (a window well in the past — same effect as time
        # having moved on past a real 07:30 end).
        monkeypatch.setattr(
            sweep, "plugin_config",
            lambda name: _config(quiet_hours=_quiet_window(-180, -120)),
        )
        counts = sweep.reconcile(db_session, [self._item()])  # still active

        assert counts["resent"] == 1
        assert len(recording.sent) == 1, "exactly one push, not a backlog dump"
        row = db_session.query(NotificationSend).one()
        assert row.suppressed_reason is None

    def test_resolving_during_quiet_hours_never_pushes(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, quiet_hours=_quiet_window(-5, 5))
        sweep.reconcile(db_session, [self._item()])

        counts = sweep.reconcile(db_session, [])  # cleared before window end

        assert counts["resolved"] == 1
        assert recording.sent == []
        row = db_session.query(NotificationSend).one()
        assert row.send_count == 0

    def test_empty_string_disables_quiet_hours(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, quiet_hours="")

        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["new"] == 1
        assert len(recording.sent) == 1

    def test_junk_quiet_hours_disables_rather_than_crashes(self, db_session, monkeypatch):
        recording = self._wire(monkeypatch, quiet_hours="not-a-window")

        counts = sweep.reconcile(db_session, [self._item()])

        assert counts["new"] == 1
        assert len(recording.sent) == 1


@pytest.mark.db
class TestCriticalBypassesAllThreeGates:
    def test_critical_pushes_immediately_regardless_of_every_gate(self, db_session, monkeypatch):
        recording = _RecordingClient()
        monkeypatch.setattr(sweep, "client", recording)
        monkeypatch.setattr(
            sweep, "plugin_config",
            lambda name: _config(
                min_active_minutes=30,
                refire_cooldown_minutes=120,
                quiet_hours=_quiet_window(-5, 5),  # currently inside the window
            ),
        )
        item = sweep.AlertItem(
            fingerprint="reauth:google:someone@example.com",
            title="t", body="b", severity="critical",
        )

        counts = sweep.reconcile(db_session, [item])

        assert counts["new"] == 1
        assert len(recording.sent) == 1
        row = db_session.query(NotificationSend).one()
        assert row.suppressed_reason is None
        assert row.send_count == 1

    def test_critical_refire_ignores_the_cooldown_too(self, db_session, monkeypatch):
        recording = _RecordingClient()
        monkeypatch.setattr(sweep, "client", recording)
        monkeypatch.setattr(
            sweep, "plugin_config", lambda name: _config(refire_cooldown_minutes=120)
        )
        item = sweep.AlertItem(
            fingerprint="reauth:google:someone@example.com",
            title="t", body="b", severity="critical",
        )
        sweep.reconcile(db_session, [item])
        sweep.reconcile(db_session, [])  # resolves

        counts = sweep.reconcile(db_session, [item])  # refires immediately

        assert counts["new"] == 1
        non_recovery = [s for s in recording.sent if s[2] != "recovery"]
        assert len(non_recovery) == 2


class TestAdHocSendIsUnaffectedByPushGating:
    """`notify_send` (`tools.handle_send`) calls `client.publish` directly —
    it never touches `reconcile`/`_push_gate`/the sweep's fingerprint-keyed
    dedup at all. A gate meant for the automated sweep must never hold back
    a human-initiated push. (It DOES write its own one-shot ledger row now
    — see `TestAdHocPublishLedgersEverySend` below — but that's an insert
    with `resolved_at` set immediately, so it can't interact with
    `reconcile`'s `WHERE resolved_at IS NULL` query either.)"""

    def test_ad_hoc_send_ignores_gate_config_entirely(self, monkeypatch, mock_session):
        from app.integrations.notifications import client as ha_client
        from app.integrations.notifications import tools

        # A config that would hold back *everything* in the sweep (always in
        # quiet hours, huge gate/cooldown) must have zero effect here.
        monkeypatch.setattr(
            ha_client, "plugin_config",
            lambda name: _config(
                targets={"1": "mobile_app_a"},
                min_active_minutes=999999,
                refire_cooldown_minutes=999999,
                quiet_hours="00:00-23:59",
            ),
        )
        monkeypatch.setattr(ha_client, "get_capability", lambda name: _FakeNotifyFacade())
        # Unit-tier (mocked session, no real Postgres) — the ledger write
        # itself needs a real DB, covered separately below.
        monkeypatch.setattr(ha_client, "_record_send", lambda *a, **k: None)
        result = json.loads(tools.handle_send(mock_session, {"message": "hello"}))
        assert result["sent"] is True


# ---------------------------------------------------------------------------
# "Ledger every publish" (2026-09-04) — `vault/Projects/lios/Backlog.md`'s
# "Ad-hoc pushes are never ledgered" item. Real Postgres needed since these
# assert on rows `client.py::_record_send` writes via its own `get_db()`
# session — `db_session`'s `real_db` fixture points that singleton at the
# test container for the duration of the test.
# ---------------------------------------------------------------------------

@pytest.mark.db
class TestAdHocPublishLedgersEverySend:
    def _wire(self, monkeypatch, fail_targets=None):
        from app.integrations.notifications import client as ha_client

        facade = _FakeNotifyFacade(fail_targets=fail_targets or set())
        # `notify_send` routes to the bound caller (user 1 under the autouse
        # `_pin_test_user`), so the per-user map is what it resolves through;
        # `household_targets` is what the sweep's `reconcile()` uses.
        monkeypatch.setattr(
            ha_client, "plugin_config",
            lambda name: _config(
                targets={"1": "mobile_app_a"}, household_targets=["mobile_app_a"],
            ),
        )
        monkeypatch.setattr(ha_client, "get_capability", lambda name: facade)
        return facade

    def test_tool_send_writes_one_row_source_tool_null_fingerprint(
        self, db_session, monkeypatch
    ):
        from app.integrations.notifications import tools

        self._wire(monkeypatch)
        result = json.loads(tools.handle_send(db_session, {"message": "hello"}))
        assert result["sent"] is True

        rows = db_session.query(NotificationSend).filter(
            NotificationSend.source == "tool"
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.fingerprint is None
        assert row.status == "sent"
        assert row.error_text is None
        assert row.resolved_at is not None, "an ad-hoc row is closed at insert time"

    def test_failing_send_writes_a_failed_row_with_error_text(
        self, db_session, monkeypatch
    ):
        from app.integrations.notifications import tools

        self._wire(monkeypatch, fail_targets={"mobile_app_a"})
        result = json.loads(tools.handle_send(db_session, {"message": "hello"}))
        assert result["sent"] is False

        row = db_session.query(NotificationSend).filter(
            NotificationSend.source == "tool"
        ).one()
        assert row.status == "failed"
        assert row.error_text
        assert row.send_count == 0

    def test_notify_recent_surfaces_the_ad_hoc_send(self, db_session, monkeypatch):
        """The whole point: `notify_recent`'s own description promises to
        answer "was I actually told?" — before this fix it could only ever
        answer that for sweep-originated rows."""
        from app.integrations.notifications import tools

        self._wire(monkeypatch)
        tools.handle_send(db_session, {"message": "hello", "title": "check"})

        result = json.loads(tools.handle_recent(db_session, {"source": "tool"}))
        assert result["count"] == 1
        row = result["notifications"][0]
        assert row["source"] == "tool"
        assert row["send_status"] == "sent"
        assert row["fingerprint"] is None

    def test_sweep_alert_still_written_exactly_once(self, db_session, monkeypatch):
        """`publish()`'s `ledger=True` default must not double-write a sweep
        alert: `sweep.py` passes `ledger=False` because `reconcile()` already
        owns that row's whole lifecycle. Runs the real `client.publish()`
        (not the `_RecordingClient` test double used elsewhere in this file)
        so a regression that dropped `ledger=False` from the sweep's call
        sites would actually be caught here."""
        from app.integrations.notifications import sweep

        self._wire(monkeypatch)
        monkeypatch.setattr(sweep, "plugin_config", lambda name: _config())
        item = sweep.AlertItem(
            fingerprint="integration:obsidian:data_stale",
            title="lios: obsidian degraded",
            body="obsidian: data stale",
            severity="warning",
        )
        sweep.reconcile(db_session, [item])

        assert db_session.query(NotificationSend).count() == 1
        row = db_session.query(NotificationSend).one()
        assert row.source == "sweep"
        assert row.fingerprint == "integration:obsidian:data_stale"
        assert row.resolved_at is None, "a live sweep alert stays open"

    def test_household_capture_confirmation_is_tagged_household(
        self, db_session, monkeypatch
    ):
        import app.plugin.capabilities as capabilities
        from app.integrations.household import tools as household_tools
        from app.integrations.notifications.facade import FACADE

        self._wire(monkeypatch)
        monkeypatch.setattr(capabilities, "get_capability", lambda name: FACADE)
        user = SimpleNamespace(id=2)
        household_tools._notify_capture_confirmation("snag", "leaking tap", user)

        row = db_session.query(NotificationSend).filter(
            NotificationSend.source == "household"
        ).one()
        assert row.user_id == 2


class TestNotifySend400Claim:
    """`edge/lunch-autopilot/README.md`, `deploy/scripts/lios-db-backup.sh`
    and `deploy/nas-backup/notify-failed.sh` say (as of comments dated
    2026-09-03) that comar's own `notify_send` "currently 400s on a payload
    bug" and call Home Assistant directly instead. The bug they're
    describing is real and already pinned by
    `tests/test_homeassistant.py::TestNotifyPayloadShape` — `data` used to
    be spread into the payload root, and HA's `notify.mobile_app_*` schema
    rejects any root key beyond `message`/`title`/`target`/`data` with
    `HTTP 400 extra keys not allowed`. But that was fixed **2026-08-22**
    (`facade.py::HomeAssistantFacade.notify`'s docstring, and that test
    class's own "verified against the live instance" note) — twelve days
    before the "currently 400s" comments were written.

    This drives the real payload-building path end to end
    (`notify_send` → `client.publish` → `homeassistant.notify` capability →
    `HomeAssistantFacade.notify` → `call_service`) with the exact payload
    shape `edge/lunch-autopilot/run.sh`'s `notify()` sends over raw curl —
    `{message, title, data: {push: {interruption-level: passive}}}`, i.e.
    `severity="recovery"` — against a fake `call_service` that 400s on any
    extra root key, the way the real HA schema does. `_FakeNotifyFacade`
    (used everywhere else in this file) stands in one level higher than the
    bug lived and would hide a regression here.
    """

    def test_recovery_severity_payload_matching_run_sh_does_not_400(
        self, monkeypatch, mock_session
    ):
        import app.integrations.homeassistant.client as ha_service_client
        from app.integrations.homeassistant.facade import HomeAssistantFacade
        from app.integrations.notifications import client as ha_client
        from app.integrations.notifications import tools

        def _strict_call_service(domain, service, data):
            allowed = {"message", "title", "target", "data"}
            extra = set(data) - allowed
            if extra:
                from app.errors import PermanentError

                raise PermanentError(f"extra keys not allowed: {sorted(extra)}")
            return True

        monkeypatch.setattr(ha_service_client, "call_service", _strict_call_service)
        monkeypatch.setattr(
            ha_client, "plugin_config",
            lambda name: _config(targets={"1": "mobile_app_a"}),
        )
        # The real facade, not `_FakeNotifyFacade` — it's the payload
        # builder under test.
        monkeypatch.setattr(ha_client, "get_capability", lambda name: HomeAssistantFacade())
        monkeypatch.setattr(ha_client, "_record_send", lambda *a, **k: None)

        result = json.loads(
            tools.handle_send(mock_session, {"message": "hello", "severity": "recovery"})
        )
        assert result["sent"] is True, result


@pytest.mark.db
class TestPushChannelHealthAxis:
    """`client.py::_record_push_channel_health` — a failing push channel
    must be visible on `system_alerts` without a new axis in
    `system/tools.py`: it writes a synthetic `sync_state` row
    (`notifications_push`) that the existing generic "consecutive_failures"
    axis (Axis 1 in `_build_alerts_payload`) already renders for every row
    in that table."""

    def _wire(self, monkeypatch, fail_targets=None):
        from app.integrations.notifications import client as ha_client

        facade = _FakeNotifyFacade(fail_targets=fail_targets or set())
        # `notify_send` routes to the bound caller (user 1 under the autouse
        # `_pin_test_user`), so the per-user map is what it resolves through;
        # `household_targets` is what the sweep's `reconcile()` uses.
        monkeypatch.setattr(
            ha_client, "plugin_config",
            lambda name: _config(
                targets={"1": "mobile_app_a"}, household_targets=["mobile_app_a"],
            ),
        )
        monkeypatch.setattr(ha_client, "get_capability", lambda name: facade)
        return facade

    def _sync_state(self, db_session, name):
        from app.models.tokens import SyncState

        return db_session.query(SyncState).filter_by(integration=name).one_or_none()

    def test_failed_ad_hoc_send_increments_push_channel_failures(
        self, db_session, monkeypatch
    ):
        from app.integrations.notifications import tools

        self._wire(monkeypatch, fail_targets={"mobile_app_a"})
        tools.handle_send(db_session, {"message": "hello"})
        tools.handle_send(db_session, {"message": "hello again"})

        state = self._sync_state(db_session, "notifications_push")
        assert state is not None
        assert state.consecutive_failures == 2
        assert state.last_sync_status == "error"

    def test_a_later_success_resets_the_failure_count(self, db_session, monkeypatch):
        from app.integrations.notifications import tools

        self._wire(monkeypatch, fail_targets={"mobile_app_a"})
        tools.handle_send(db_session, {"message": "hello"})

        self._wire(monkeypatch)  # fix the channel
        tools.handle_send(db_session, {"message": "hello again"})

        state = self._sync_state(db_session, "notifications_push")
        assert state.consecutive_failures == 0
        assert state.last_sync_status == "ok"


# ---------------------------------------------------------------------------
# 2026-09-06 — one credential: `notify_send` pushes to the CALLER only
# ---------------------------------------------------------------------------

class TestNotifySendRoutesToCaller:
    """Alex's decision (2026-09-06): lios has one credential, the per-user
    bearer. `notify_send` used to take `user_id` from its *arguments* and
    push to that phone with no check that the caller was that user — any
    bound caller could buzz anyone. The recipient is now the bound caller;
    `household=true` is the only other shape and means `publish()`'s
    existing `user_id=None` fan-out. Pushes to *another* person stay a
    server-side decision (`client.publish(..., user_id=...)` from tasks /
    inbox / absence code), never a tool argument."""

    def _capture(self, monkeypatch):
        from app.integrations.notifications import client as ha_client
        from app.integrations.notifications import tools

        seen: dict = {}

        def _publish(title, body, severity="warning", user_id=None, **kwargs):
            seen["user_id"] = user_id

        monkeypatch.setattr(tools.client, "publish", _publish)
        return tools, seen

    def test_send_routes_to_the_bound_caller(self, monkeypatch, mock_session):
        from app.auth.context import use_user

        tools, seen = self._capture(monkeypatch)
        with use_user(2):
            result = json.loads(tools.handle_send(mock_session, {"message": "hello"}))

        assert result["sent"] is True
        assert seen["user_id"] == 2

    def test_a_user_id_argument_cannot_redirect_the_push(self, monkeypatch, mock_session):
        """The old schema's `user_id` is ignored if a stale client still
        sends it — the push goes to whoever is calling, not whoever is
        named."""
        from app.auth.context import use_user

        tools, seen = self._capture(monkeypatch)
        with use_user(2):
            tools.handle_send(mock_session, {"message": "hello", "user_id": 1})

        assert seen["user_id"] == 2

    def test_household_true_fans_out_household_wide(self, monkeypatch, mock_session):
        from app.auth.context import use_user

        tools, seen = self._capture(monkeypatch)
        with use_user(2):
            tools.handle_send(mock_session, {"message": "hello", "household": True})

        assert seen["user_id"] is None

    def test_unbound_caller_cannot_send(self, monkeypatch, mock_session):
        """No caller identity means no recipient — the tool must not fall
        back to "household" (or worse, user 1) for an unattributed call."""
        from app.auth.context import _current_user_id

        tools, seen = self._capture(monkeypatch)
        token = _current_user_id.set(0)
        try:
            with pytest.raises(RuntimeError):
                tools.handle_send(mock_session, {"message": "hello"})
        finally:
            _current_user_id.reset(token)
        assert "user_id" not in seen

    def test_schema_has_no_user_id_argument(self):
        from app.integrations.notifications.tools import get_mcp_tools

        send = next(t for t in get_mcp_tools() if t["name"] == "notify_send")
        props = send["inputSchema"]["properties"]
        assert "user_id" not in props
        assert "household" in props
        assert "user_id" not in send["description"]
