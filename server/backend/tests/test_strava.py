"""Tests for the Strava integration.

The centre of gravity here is deliberately the *silent* failures — the ones
that report success and lose data:

  - a refresh that doesn't persist Strava's rotated refresh token (works for
    six hours, then dead forever);
  - a consent that granted `activity:read` instead of `activity:read_all`
    (every private activity omitted, no error anywhere);
  - a backfill that reports "complete" when it was actually rate-limited;
  - a backfill that loops forever on identical start timestamps.

Correct-path parsing and unit conversion are covered too, but those fail
loudly on their own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.auth.encryption import decrypt_token, encrypt_token
from app.errors import NeedsReauthError, PermanentError
from app.integrations.strava import client as strava_client
from app.integrations.strava.models import StravaActivity
from app.integrations.strava.sync import (
    PROVIDER,
    access_token_for,
    backfill_activities,
    parse_activity,
    store_activities,
)
from app.integrations.strava.tools import _activity_to_dict, _pace_per_km
from app.models.tokens import OAuthToken


def _raw(activity_id: int = 1, **overrides) -> dict:
    """A SummaryActivity as Strava actually returns one."""
    raw = {
        "id": activity_id,
        "name": "Morning Run",
        "type": "Run",
        "sport_type": "Run",
        "start_date": "2026-08-01T06:30:00Z",
        "start_date_local": "2026-08-01T07:30:00Z",
        "timezone": "(GMT+00:00) Europe/Dublin",
        "utc_offset": 3600,
        "distance": 10000.0,
        "moving_time": 3000,
        "elapsed_time": 3120,
        "total_elevation_gain": 85.0,
        "average_speed": 3.3333,
        "max_speed": 4.5,
        "average_heartrate": 152.0,
        "max_heartrate": 176.0,
        "start_latlng": [53.1, -6.07],
        "end_latlng": [53.11, -6.08],
        "map": {"summary_polyline": "abc123"},
        "kudos_count": 4,
        "achievement_count": 1,
        "pr_count": 0,
        "trainer": False,
        "commute": False,
        "manual": False,
        "private": False,
    }
    raw.update(overrides)
    return raw


# ---------------------------------------------------------------------------
# Parsing and units
# ---------------------------------------------------------------------------


def test_parse_activity_preserves_source_units():
    """Metres and seconds go in unconverted — conversion is a read concern."""
    parsed = parse_activity(_raw(), user_id=1)

    assert parsed["distance_m"] == 10000.0
    assert parsed["moving_time_s"] == 3000
    assert parsed["average_speed_ms"] == pytest.approx(3.3333)
    assert parsed["strava_id"] == 1
    assert parsed["user_id"] == 1
    assert parsed["start_date"] == datetime(2026, 8, 1, 6, 30, tzinfo=timezone.utc)
    assert parsed["utc_offset_seconds"] == 3600
    assert parsed["map_polyline"] == "abc123"
    assert parsed["start_lat"] == 53.1
    # SourcedRecordMixin fields are populated so cross-source queries work
    # without knowing this table's column names.
    assert parsed["source_id"] == "1"
    assert parsed["source_ts"] == parsed["start_date"]


def test_parse_activity_handles_missing_gps():
    """A treadmill run has `start_latlng: []`, not a pair."""
    parsed = parse_activity(_raw(start_latlng=[], end_latlng=None), user_id=1)
    assert parsed["start_lat"] is None
    assert parsed["end_lng"] is None


def test_content_hash_ignores_social_counters():
    """Kudos move when someone else clicks; that is not a content change.

    Without this, every historical activity would be rewritten on every sync
    and the "written" count would be meaningless.
    """
    a = parse_activity(_raw(kudos_count=4), user_id=1)
    b = parse_activity(_raw(kudos_count=99), user_id=1)
    assert a["content_hash"] == b["content_hash"]

    c = parse_activity(_raw(distance=12000.0), user_id=1)
    assert a["content_hash"] != c["content_hash"]


def test_pace_is_none_rather_than_zero_without_distance():
    """A gym session has no pace. `0:00` would read as impossibly fast."""
    assert _pace_per_km(0, 3000) is None
    assert _pace_per_km(10000, 0) is None
    assert _pace_per_km(10000, 3000) == "5:00"


def test_activity_to_dict_converts_units_and_flags_estimated_power():
    row = StravaActivity(**parse_activity(
        _raw(average_watts=210.0, device_watts=False), user_id=1
    ))
    result = _activity_to_dict(row)

    assert result["distance_km"] == 10.0
    assert result["moving_time_min"] == 50.0
    assert result["avg_speed_kmh"] == 12.0
    assert result["pace_per_km"] == "5:00"
    # Estimated watts must not be presentable as measured ones.
    assert result["avg_watts"] == 210
    assert result["watts_measured"] is False


def test_activity_to_dict_applies_utc_offset_for_local_start():
    row = StravaActivity(**parse_activity(_raw(), user_id=1))
    # 06:30Z + 3600s = 07:30 local
    assert _activity_to_dict(row)["local_start"] == "2026-08-01 07:30"


# ---------------------------------------------------------------------------
# Token refresh — the rotation trap
# ---------------------------------------------------------------------------


def _token(db_session, *, expires_in_seconds: int = -60) -> OAuthToken:
    token = OAuthToken(
        provider=PROVIDER,
        user_id=1,
        account_email="12345",
        access_token=encrypt_token("old-access"),
        refresh_token=encrypt_token("old-refresh"),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
    )
    db_session.add(token)
    db_session.commit()
    return token


@pytest.mark.db
def test_refresh_persists_the_rotated_refresh_token(db_session):
    """⚠️ The failure this guards is invisible for six hours.

    Strava returns a NEW refresh token and invalidates the old one. Storing
    only `access_token` leaves a row that authenticates perfectly until the
    fresh access token expires, and is then permanently dead — with the error
    surfacing hours later and nowhere near the code that caused it.
    """
    token = _token(db_session)

    with patch.object(
        strava_client,
        "refresh_access_token",
        return_value={
            "access_token": "new-access",
            "refresh_token": "ROTATED-refresh",
            "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=6)).timestamp()),
        },
    ), patch(
        "app.integrations.strava.sync._credentials", return_value=("cid", "secret")
    ):
        result = access_token_for(db_session, token)

    assert result == "new-access"
    assert decrypt_token(token.access_token) == "new-access"
    assert decrypt_token(token.refresh_token) == "ROTATED-refresh"
    assert token.expires_at > datetime.now(timezone.utc)


@pytest.mark.db
def test_valid_token_is_not_refreshed(db_session):
    """A token with hours left must not burn an API call."""
    token = _token(db_session, expires_in_seconds=3600)

    with patch.object(strava_client, "refresh_access_token") as refresh:
        assert access_token_for(db_session, token) == "old-access"
    refresh.assert_not_called()


@pytest.mark.db
def test_near_expiry_token_is_refreshed_within_the_skew(db_session):
    """Inside REFRESH_SKEW the token is still 'valid' but must be replaced."""
    token = _token(db_session, expires_in_seconds=120)

    with patch.object(
        strava_client,
        "refresh_access_token",
        return_value={"access_token": "fresh", "refresh_token": "r2", "expires_at": 0},
    ), patch(
        "app.integrations.strava.sync._credentials", return_value=("cid", "secret")
    ):
        assert access_token_for(db_session, token) == "fresh"


@pytest.mark.db
def test_revoked_grant_is_stamped_not_retried(db_session):
    """A revoked grant must stop the scheduler retrying it every 30 minutes."""
    token = _token(db_session)

    with patch.object(
        strava_client,
        "refresh_access_token",
        side_effect=NeedsReauthError("12345", "revoked"),
    ), patch(
        "app.integrations.strava.sync._credentials", return_value=("cid", "secret")
    ):
        with pytest.raises(NeedsReauthError):
            access_token_for(db_session, token)

    db_session.refresh(token)
    assert token.needs_reauth_at is not None
    assert "revoked" in token.needs_reauth_reason

    # And a subsequent call refuses immediately, without an API round trip.
    with patch.object(strava_client, "refresh_access_token") as refresh:
        with pytest.raises(NeedsReauthError):
            access_token_for(db_session, token)
    refresh.assert_not_called()


def test_client_classifies_a_400_refresh_as_needing_reauth():
    """Strava answers a dead refresh token with 400, not 401.

    Classified as a generic PermanentError it would look like a bug to fix
    rather than a grant to re-authorise.
    """
    import httpx

    with patch("httpx.Client") as client_cls:
        client_cls.return_value.__enter__.return_value.post.return_value = httpx.Response(
            400, json={"message": "Bad Request"}, request=httpx.Request("POST", "https://x")
        )
        with pytest.raises(NeedsReauthError):
            strava_client.refresh_access_token(
                client_id="c", client_secret="s", refresh_token="dead"
            )


def test_scope_constant_requests_read_all():
    """`activity:read` silently omits private activities. Guard the constant."""
    assert "activity:read_all" in strava_client.SCOPES


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_store_is_idempotent_and_reports_only_real_changes(db_session):
    records = [parse_activity(_raw(1), user_id=1), parse_activity(_raw(2), user_id=1)]

    assert store_activities(db_session, records) == 2
    # Re-running the same window writes nothing — the count means something.
    assert store_activities(db_session, records) == 0
    assert db_session.query(StravaActivity).count() == 2

    changed = [parse_activity(_raw(1, name="Renamed"), user_id=1)]
    assert store_activities(db_session, changed) == 1
    assert db_session.query(StravaActivity).count() == 2


@pytest.mark.db
def test_store_keeps_kudos_current_without_counting_a_change(db_session):
    store_activities(db_session, [parse_activity(_raw(1, kudos_count=1), user_id=1)])
    assert store_activities(db_session, [parse_activity(_raw(1, kudos_count=7), user_id=1)]) == 0

    row = db_session.query(StravaActivity).filter_by(strava_id=1).one()
    assert row.kudos_count == 7


@pytest.mark.db
def test_two_athletes_do_not_collide_on_the_same_activity_id(db_session):
    """Composite (user_id, strava_id) uniqueness, not bare strava_id."""
    store_activities(db_session, [
        parse_activity(_raw(999), user_id=1),
        parse_activity(_raw(999), user_id=2),
    ])
    assert db_session.query(StravaActivity).count() == 2


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_backfill_walks_backwards_and_reports_complete_only_on_an_empty_page(db_session):
    _token(db_session, expires_in_seconds=3600)

    pages = [
        [_raw(3, start_date="2026-08-03T06:00:00Z"), _raw(2, start_date="2026-08-02T06:00:00Z")],
        [_raw(1, start_date="2026-08-01T06:00:00Z")],
        [],
    ]
    calls: list[int | None] = []

    def fake_fetch(*, access_token, before=None, after=None, page=1, per_page=200, account="x"):
        calls.append(before)
        return pages.pop(0)

    with patch.object(strava_client, "fetch_activities", side_effect=fake_fetch), patch(
        "app.integrations.strava.sync._credentials", return_value=("cid", "secret")
    ):
        result = backfill_activities(db_session, user_id=1, resume=False)

    assert result["status"] == "complete"
    assert result["fetched"] == 3
    assert result["written"] == 3
    # The cursor moved strictly backwards, anchored on the oldest start seen.
    assert calls[1] == int(datetime(2026, 8, 2, 6, tzinfo=timezone.utc).timestamp())
    assert calls[2] == int(datetime(2026, 8, 1, 6, tzinfo=timezone.utc).timestamp())


@pytest.mark.db
def test_backfill_reports_rate_limited_rather_than_complete(db_session):
    """⚠️ A partial archive that says 'complete' is the silent failure.

    The first page must still be persisted — the requests were already spent.
    """
    _token(db_session, expires_in_seconds=3600)

    responses = [
        [_raw(2, start_date="2026-08-02T06:00:00Z")],
        strava_client.RateLimited("limit", limit="200", usage="200"),
    ]

    def fake_fetch(*, access_token, before=None, after=None, page=1, per_page=200, account="x"):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with patch.object(strava_client, "fetch_activities", side_effect=fake_fetch), patch(
        "app.integrations.strava.sync._credentials", return_value=("cid", "secret")
    ):
        result = backfill_activities(db_session, user_id=1, resume=False)

    assert result["status"] == "rate_limited"
    assert result["written"] == 1
    assert db_session.query(StravaActivity).count() == 1


@pytest.mark.db
def test_backfill_stalls_rather_than_looping_on_identical_timestamps(db_session):
    """Several activities sharing a start time would freeze the `before` cursor.

    Without the seen-id guard this is an infinite loop that never returns.
    """
    _token(db_session, expires_in_seconds=3600)

    same = [_raw(1, start_date="2026-08-01T06:00:00Z"), _raw(2, start_date="2026-08-01T06:00:00Z")]

    with patch.object(strava_client, "fetch_activities", return_value=same), patch(
        "app.integrations.strava.sync._credentials", return_value=("cid", "secret")
    ):
        result = backfill_activities(db_session, user_id=1, resume=False, max_pages=50)

    assert result["status"] == "stalled"
    assert result["pages"] == 2


@pytest.mark.db
def test_backfill_without_a_connected_account_is_a_clear_error(db_session):
    with patch(
        "app.integrations.strava.sync._credentials", return_value=("cid", "secret")
    ):
        with pytest.raises(PermanentError, match="No Strava account connected"):
            backfill_activities(db_session, user_id=1, resume=False)


# ---------------------------------------------------------------------------
# Integration wiring
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_accounts_excludes_rows_needing_reauth(db_session):
    """A dead grant must not raise on every scheduled run and bury a real
    failure on the other athlete's row."""
    from app.integrations.strava import StravaIntegration

    live = _token(db_session, expires_in_seconds=3600)
    dead = OAuthToken(
        provider=PROVIDER,
        user_id=2,
        account_email="67890",
        access_token=encrypt_token("x"),
        refresh_token=encrypt_token("y"),
        needs_reauth_at=datetime.now(timezone.utc),
        needs_reauth_reason="revoked",
    )
    db_session.add(dead)
    db_session.commit()

    accounts = StravaIntegration().accounts(db_session)
    assert [a.account_email for a in accounts] == [live.account_email]


def test_missing_credentials_name_the_missing_keys():
    """Failing at the call site, not by gating the whole integration off."""
    from app.integrations.strava.sync import _credentials
    from unittest.mock import MagicMock

    with patch(
        "app.integrations.strava.sync.plugin_config",
        return_value=MagicMock(strava_client_id="", strava_client_secret=""),
    ):
        with pytest.raises(PermanentError) as excinfo:
            _credentials()

    assert "strava_client_id" in str(excinfo.value)
    assert "strava_client_secret" in str(excinfo.value)


def test_manifest_declares_its_model_and_routes():
    from app.integrations.strava.manifest import MANIFEST

    assert MANIFEST.name == "strava"
    assert MANIFEST.models == ["StravaActivity"]
    assert MANIFEST.routes == ["app.integrations.strava.routes:router"]
    # `oauth` is the kernel's GOOGLE scope union — Strava must not join it.
    assert MANIFEST.oauth is None


def test_callback_is_exempt_from_ui_token_auth():
    """The OAuth return leg must not be gated behind the UI token.

    ⚠️ Found only by running the flow, never by the unit tests: `ui_token` is
    set SameSite=Strict, so when Strava redirects the BROWSER back after
    consent, the cookie is not sent on that cross-site navigation. Gated, the
    callback 401s and the grant the user just approved is lost — and the
    failure looks like a Strava problem, not a comar one. `/api/auth/google/
    callback` carries the same exemption for the same reason.

    Exempt is not unauthenticated: the callback verifies an HMAC-signed,
    10-minute `state` before it will write a token row.
    """
    from app.main import AUTH_EXEMPT, AUTH_EXEMPT_PREFIXES

    path = "/api/strava/callback"
    exempt = path in AUTH_EXEMPT or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES)
    assert exempt, "strava OAuth callback must be reachable without the UI token"


def test_connect_is_NOT_exempt_from_ui_token_auth():
    """The other half of the pair, and the reason this isn't a blanket exemption.

    `connect` names the user a token will be attributed to. It initiates the
    flow rather than completing one, carries no signed state to verify, and is
    reached by a normal same-site click that does send the cookie — so it stays
    gated. Exempting the whole `/api/strava/` prefix would have taken this with
    it and let anyone on the tailnet start a grant against any user's row.
    """
    from app.main import AUTH_EXEMPT, AUTH_EXEMPT_PREFIXES

    path = "/api/strava/connect"
    exempt = path in AUTH_EXEMPT or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES)
    assert not exempt, "strava connect must stay behind the UI token"
