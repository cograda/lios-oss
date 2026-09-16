"""`system_alerts` per-user vs household scoping (F-alerts-scoping).

The bug: `handle_alerts` used to run one query with no notion of who was
asking, so Sam's `/daily-note` surfaced a failing commute sync and a gap in
Alex's Apple Health data — integrations/data that are not hers. Two
consumers have opposite requirements:

  - `handle_alerts` (the `system_alerts` MCP tool / daily brief) must scope
    down to whichever user is bound, or fall back to the household view when
    nothing is bound.
  - `handle_alerts_household` (used by the notifications sweep and the
    dashboard route) must always see everything, independent of any ambient
    user context.

These tests pin: a per-user integration broken for user A stays invisible to
user B; household infrastructure is visible to everyone regardless of scope;
and the household view sees every user's issues even with nothing bound.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.auth.context import use_user

pytestmark = pytest.mark.db


def _seed_sync_state(session, *, integration, consecutive_failures=1, status="error"):
    from app.models.tokens import SyncState

    session.add(SyncState(
        integration=integration,
        last_sync_status=status,
        consecutive_failures=consecutive_failures,
        last_sync_at=datetime.now(timezone.utc) - timedelta(hours=6),
    ))
    session.commit()


def _seed_health_gap(session, user_id, days_ago_list):
    """Rows with a hole in the middle, so `coverage_gaps` reports it."""
    from app.integrations.apple_health.models import HealthDailyMetric

    today = date.today()
    for n in days_ago_list:
        session.add(HealthDailyMetric(
            user_id=user_id,
            date=today - timedelta(days=n),
            metric_type="steps",
            value=1000 + n,
            synced_at=datetime.now(timezone.utc),
        ))
    session.commit()


def _seed_mail(session, user_id):
    from app.integrations.google_mail.models import MailMessage

    session.add(MailMessage(
        user_id=user_id,
        google_message_id=f"msg-{user_id}",
        thread_id=f"thread-{user_id}",
        account_email="someone@example.com",
        subject="hi",
        date=datetime.now(timezone.utc),
    ))
    session.commit()


def _seed_oauth_token(session, user_id, *, needs_reauth=True):
    from app.models.tokens import OAuthToken

    token = OAuthToken(
        user_id=user_id,
        provider="google",
        account_email=f"user{user_id}@example.com",
        access_token="dummy",
        refresh_token="dummy",
    )
    if needs_reauth:
        token.needs_reauth_at = datetime.now(timezone.utc)
        token.needs_reauth_reason = "revoked"
    session.add(token)
    session.commit()


class TestPerUserIntegrationScoping:
    """A per-user integration broken for user A must not appear for user B,
    but must still reach the household (unbound) view."""

    def test_health_gap_for_one_user_not_shown_to_the_other(self, db_session):
        from app.integrations.system.tools import handle_alerts

        # Alex (1) has a hole; Sam (2) has nothing at all.
        present = [n for n in range(1, 15) if n not in (5, 6, 7)]
        _seed_health_gap(db_session, 1, present)

        with use_user(2):
            payload = json.loads(handle_alerts(db_session, {}))
        assert not any(a["integration"] == "apple_health" for a in payload["alerts"])

        with use_user(1):
            payload = json.loads(handle_alerts(db_session, {}))
        entry = next(a for a in payload["alerts"] if a["integration"] == "apple_health")
        assert any("data gap for user 1" in issue for issue in entry["issues"])
        assert not any("data gap for user 2" in issue for issue in entry["issues"])

    def test_health_gap_visible_to_its_own_user_even_if_other_has_data(self, db_session):
        """Sam having her own (complete) health data must not hide Alex's
        gap from Alex, nor leak it to her."""
        from app.integrations.system.tools import handle_alerts

        present = [n for n in range(1, 15) if n not in (5, 6, 7)]
        _seed_health_gap(db_session, 1, present)
        _seed_health_gap(db_session, 2, range(1, 15))  # Sam: complete, no gap

        with use_user(2):
            payload = json.loads(handle_alerts(db_session, {}))
        entry = next((a for a in payload["alerts"] if a["integration"] == "apple_health"), None)
        assert entry is None or not entry["issues"]

        with use_user(1):
            payload = json.loads(handle_alerts(db_session, {}))
        entry = next(a for a in payload["alerts"] if a["integration"] == "apple_health")
        assert any("data gap for user 1" in issue for issue in entry["issues"])

    def test_sync_failure_for_unused_integration_hidden_from_caller(self, db_session):
        """google_mail is a per-user integration (UserOwnedMixin MailMessage).
        A caller with zero mail data of their own shouldn't see its sync
        failure — that's the literal shape of the reported bug (commute /
        Apple Health noise for an integration the caller never touches)."""
        from app.integrations.system.tools import handle_alerts

        _seed_sync_state(db_session, integration="google_mail")

        with use_user(2):  # Sam has no MailMessage rows at all
            payload = json.loads(handle_alerts(db_session, {}))
        assert not any(a["integration"] == "google_mail" for a in payload["alerts"])

    def test_sync_failure_for_used_integration_still_shown_to_owner(self, db_session):
        from app.integrations.system.tools import handle_alerts

        _seed_sync_state(db_session, integration="google_mail")
        _seed_mail(db_session, user_id=1)

        with use_user(1):
            payload = json.loads(handle_alerts(db_session, {}))
        entry = next(a for a in payload["alerts"] if a["integration"] == "google_mail")
        assert any("failing" in issue for issue in entry["issues"])

    def test_reauth_only_shown_to_owning_user(self, db_session):
        from app.integrations.system.tools import handle_alerts

        _seed_oauth_token(db_session, user_id=1)

        with use_user(2):
            payload = json.loads(handle_alerts(db_session, {}))
        assert payload["reauth_needed"] == []

        with use_user(1):
            payload = json.loads(handle_alerts(db_session, {}))
        assert len(payload["reauth_needed"]) == 1
        assert payload["reauth_needed"][0]["user_id"] == 1


class TestHouseholdInfrastructureAlwaysVisible:
    """Integrations with no UserOwnedMixin model (commute, homeassistant,
    weather, finance, ...) are household infrastructure — every caller sees
    them regardless of whether they personally "use" that integration."""

    @pytest.mark.parametrize("name", ["commute", "homeassistant", "weather", "finance"])
    def test_household_integration_not_per_user(self, name):
        from app.integrations.system.tools import _per_user_integration_names

        assert name not in _per_user_integration_names()

    def test_household_alert_visible_to_both_users(self, db_session):
        from app.integrations.system.tools import handle_alerts

        _seed_sync_state(db_session, integration="commute")

        for uid in (1, 2):
            with use_user(uid):
                payload = json.loads(handle_alerts(db_session, {}))
            entry = next(a for a in payload["alerts"] if a["integration"] == "commute")
            assert any("failing" in issue for issue in entry["issues"])


class TestPerUserIntegrationDerivation:
    """The per-user/household split is derived from `UserOwnedMixin`, not
    hand-listed — this pins the known cases on both sides."""

    def test_known_per_user_integrations(self):
        from app.integrations.system.tools import _per_user_integration_names

        names = _per_user_integration_names()
        for expected in ("apple_health", "google_mail", "whatsapp", "lastfm", "coffee"):
            assert expected in names, f"{expected} should be derived as per-user"


class TestHouseholdViewSeesEverything:
    """`handle_alerts_household` must never depend on ambient user context —
    the notifications sweep runs on a cron with nothing bound at all, and
    must still see every user's issues."""

    def test_unbound_household_view_sees_both_users_health_gaps(self, db_session):
        from app.integrations.system.tools import handle_alerts_household

        _seed_health_gap(db_session, 1, [n for n in range(1, 15) if n != 5])
        _seed_health_gap(db_session, 2, [n for n in range(1, 15) if n != 9])

        # `use_user(0)` is the documented sentinel for genuinely unbound —
        # conftest's autouse fixture otherwise pins user 1 for every test.
        with use_user(0):
            payload = json.loads(handle_alerts_household(db_session, {}))
        entry = next(a for a in payload["alerts"] if a["integration"] == "apple_health")
        bodies = " ".join(entry["issues"])
        assert "data gap for user 1" in bodies
        assert "data gap for user 2" in bodies

    def test_unbound_household_view_ignores_ambient_binding(self, db_session):
        """Even if something is bound (e.g. a stray context), the *household*
        entry point must not scope down — that's the whole point of it being
        a separate, explicit method rather than reading the ContextVar."""
        from app.integrations.system.tools import handle_alerts_household

        _seed_sync_state(db_session, integration="google_mail")
        _seed_mail(db_session, user_id=1)

        with use_user(2):  # bound to a user with no mail data at all
            payload = json.loads(handle_alerts_household(db_session, {}))
        assert any(a["integration"] == "google_mail" for a in payload["alerts"])

    def test_sweep_uses_the_household_entry_point(self, monkeypatch, mock_session):
        """The cron must call the context-independent method, not `alerts()`
        — regression guard for the exact bug this fix targets: the sweep
        silently scoping down if it ever runs with something bound."""
        from app.integrations.notifications import sweep

        calls = []

        class _Facade:
            def alerts_household(self, session, arguments):
                calls.append(arguments)
                return json.dumps({"alerts": [], "reauth_needed": [], "tool_alerts": []})

        monkeypatch.setattr(sweep, "get_capability", lambda name: _Facade())
        # Must carry every key `collect()` reads, not just the one this test
        # cares about — `plugin_config()` returns a validated model with the
        # full schema, so a stub missing a field tests a shape that cannot
        # occur. Deliberately not made tolerant with `getattr(..., default)`
        # in `sweep`: a key dropped from the manifest should fail loudly.
        monkeypatch.setattr(
            sweep, "plugin_config",
            lambda name: type("Cfg", (), {
                "threshold_minutes": 60,
                "suppress_push_for_user_ids": [],
                "sleep_deadline_user_ids": [],
                "sleep_deadline_hour": 10,
                "sleep_deadline_timezone": "Europe/Dublin",
            })(),
        )

        sweep.collect(mock_session)
        assert calls  # alerts_household was actually invoked


class TestUnmeasuredIntegrationsSurfaced:
    """`system_alerts`/`handle_alerts_household` must name every integration
    with no staleness probe at all — see `data_freshness.unmeasured_integrations`.
    Without this, an integration like `finance` with 143 days of no data reads
    as part of an "all_ok" panel, because `data_freshness.check_all` simply
    never mentions anything it was never told to probe.

    Household-wide fact, not per-user: whether an integration is instrumented
    at all doesn't belong to whichever caller happened to ask, so this must
    show up identically scoped and unscoped.
    """

    def test_unmeasured_list_present_and_names_finance(self, db_session):
        from app.integrations.system.tools import handle_alerts

        payload = json.loads(handle_alerts(db_session, {}))
        assert "finance" in payload["unmeasured"]

    def test_probed_integration_not_in_unmeasured_list(self, db_session):
        from app.integrations.system.tools import handle_alerts

        payload = json.loads(handle_alerts(db_session, {}))
        assert "lastfm" not in payload["unmeasured"]

    def test_commute_absent_from_unmeasured_despite_window_gating(self, db_session):
        """commute HAS a probe — it's merely excluded from `check_all`'s
        result outside its weekday-morning window. That is a different state
        from never having been instrumented, so it must never appear here."""
        from app.integrations.system.tools import handle_alerts

        payload = json.loads(handle_alerts(db_session, {}))
        assert "commute" not in payload["unmeasured"]

    def test_unmeasured_identical_scoped_and_household(self, db_session):
        from app.integrations.system.tools import handle_alerts, handle_alerts_household

        with use_user(2):
            scoped = json.loads(handle_alerts(db_session, {}))
        household = json.loads(handle_alerts_household(db_session, {}))
        assert set(scoped["unmeasured"]) == set(household["unmeasured"])
