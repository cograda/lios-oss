"""Commute integration tests.

Unit tier: pure client-layer parsing helpers (GTFS time parsing, HH:MM
parsing, predict_stop_time confidence tiers).
db tier: sync_commute against real Postgres, with the two feed fetches and
the HA push monkeypatched — exercises persistence + the ha_pushed flag +
HA-down tolerance.
"""

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

import json

from app.errors import PermanentError, TransientError
from app.integrations.commute.client import gtfs_time_to_datetime, hhmm_to_datetime, predict_stop_time
from app.integrations.commute.domain import BusDeparture, DartService
from app.integrations.commute.models import CommuteDecision
from app.integrations.commute.sync import sync_commute
from app.integrations.commute.tools import handle_query


# ---------------------------------------------------------------------------
# Unit tier — pure client-layer parsing
# ---------------------------------------------------------------------------

class TestGtfsTimeToDatetime:
    def test_normal_time(self):
        ref = datetime(2026, 7, 3, 6, 0)
        assert gtfs_time_to_datetime("08:15:30", ref) == datetime(2026, 7, 3, 8, 15, 30)

    def test_past_midnight_gtfs_convention(self):
        # GTFS allows >24:00:00 for trips that run past midnight on the same service day.
        ref = datetime(2026, 7, 3, 6, 0)
        result = gtfs_time_to_datetime("25:05:00", ref)
        assert result == datetime(2026, 7, 4, 1, 5, 0)


class TestHhmmToDatetime:
    def test_normal(self):
        ref = datetime(2026, 7, 3, 8, 0)
        assert hhmm_to_datetime("08:35", ref) == datetime(2026, 7, 3, 8, 35)

    def test_no_estimate_returns_none(self):
        ref = datetime(2026, 7, 3, 8, 0)
        assert hhmm_to_datetime("00:00", ref) is None
        assert hhmm_to_datetime("", ref) is None
        assert hhmm_to_datetime(None, ref) is None

    def test_far_before_ref_rolls_to_next_day(self):
        # A time appearing >6h before ref is treated as tomorrow, not today.
        ref = datetime(2026, 7, 3, 8, 0)
        result = hhmm_to_datetime("00:30", ref)
        assert result == datetime(2026, 7, 4, 0, 30)


@dataclass
class _FakeStopTime:
    stop_id: str
    stop_sequence: int
    departure: "_FakeEvent"
    arrival: "_FakeEvent"


@dataclass
class _FakeEvent:
    time: int = 0
    delay: int = 0


class TestPredictStopTime:
    def test_exact_match_with_time_is_live(self):
        scheduled = datetime(2026, 7, 3, 8, 15)
        stu = [_FakeStopTime("howth", 5, _FakeEvent(time=1751530500), _FakeEvent())]
        dt, confidence, delay = predict_stop_time(stu, 5, scheduled)
        assert confidence == "live"
        assert dt is not None

    def test_exact_match_with_delay_only(self):
        scheduled = datetime(2026, 7, 3, 8, 15)
        stu = [_FakeStopTime("howth", 5, _FakeEvent(time=0, delay=300), _FakeEvent(delay=0))]
        dt, confidence, delay = predict_stop_time(stu, 5, scheduled)
        assert confidence == "live"
        assert dt == datetime(2026, 7, 3, 8, 20)
        assert delay == 5.0

    def test_propagated_from_earlier_stop(self):
        scheduled = datetime(2026, 7, 3, 8, 15)
        stu = [_FakeStopTime("home", 2, _FakeEvent(delay=120), _FakeEvent())]
        dt, confidence, delay = predict_stop_time(stu, 5, scheduled)
        assert confidence == "propagated"
        assert dt == datetime(2026, 7, 3, 8, 17)

    def test_extrapolated_back_from_later_stop(self):
        scheduled = datetime(2026, 7, 3, 8, 15)
        stu = [_FakeStopTime("later", 9, _FakeEvent(delay=60), _FakeEvent())]
        dt, confidence, delay = predict_stop_time(stu, 5, scheduled)
        assert confidence == "extrapolated-back"

    def test_no_stop_time_updates_is_scheduled(self):
        scheduled = datetime(2026, 7, 3, 8, 15)
        dt, confidence, delay = predict_stop_time([], 5, scheduled)
        assert dt is None
        assert confidence == "scheduled"


# ---------------------------------------------------------------------------
# Unit tier — NTA TLS trust chain
#
# Regression cover for 2026-07-29: api.nationaltransport.ie sits behind Azure
# Traffic Manager and a subset of its endpoints serve the leaf cert only,
# omitting the GoDaddy G2 intermediate. certifi has the root but not the
# intermediate, so ~43% of calls died with CERTIFICATE_VERIFY_FAILED. We ship
# the intermediate and build the trust store from certifi + that file.
# ---------------------------------------------------------------------------

class TestNtaTrustChain:
    def test_intermediate_pem_ships_and_is_valid(self):
        """The PEM must exist on disk (it's COPYed into the image) and parse."""
        from cryptography import x509

        from app.integrations.commute.client import _NTA_INTERMEDIATE_PATH

        assert _NTA_INTERMEDIATE_PATH.exists(), _NTA_INTERMEDIATE_PATH
        cert = x509.load_pem_x509_certificate(_NTA_INTERMEDIATE_PATH.read_bytes())
        assert "Go Daddy Secure Certificate Authority" in cert.subject.rfc4514_string()

    def test_intermediate_not_expired(self):
        """A silently-expired intermediate would reintroduce the outage."""
        from cryptography import x509

        from app.integrations.commute.client import _NTA_INTERMEDIATE_PATH

        cert = x509.load_pem_x509_certificate(_NTA_INTERMEDIATE_PATH.read_bytes())
        assert cert.not_valid_after_utc > datetime.now(timezone.utc), (
            "shipped NTA intermediate has expired — refetch from the leaf's AIA URL"
        )

    def test_ssl_context_trusts_intermediate_and_root(self):
        """The context must carry BOTH our intermediate and certifi's roots —
        loading only the intermediate would break every other chain."""
        from app.integrations.commute.client import _nta_ssl_context

        subjects = [
            str(entry)
            for cert in _nta_ssl_context().get_ca_certs()
            for rdn in cert.get("subject", ())
            for entry in rdn
        ]
        blob = " ".join(subjects)
        assert "Go Daddy Secure Certificate Authority - G2" in blob  # ours
        assert "Go Daddy Root Certificate Authority - G2" in blob    # certifi's
        assert len(_nta_ssl_context().get_ca_certs()) > 100          # full bundle

    def test_ssl_context_is_cached(self):
        from app.integrations.commute.client import _nta_ssl_context

        assert _nta_ssl_context() is _nta_ssl_context()

    def test_fetch_retries_connect_error_then_succeeds(self):
        """A sick Traffic Manager node must not fail the whole call."""
        from app.integrations.commute import client as commute_client

        calls = []

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                calls.append(url)
                if len(calls) == 1:
                    raise httpx.ConnectError("CERTIFICATE_VERIFY_FAILED")
                return httpx.Response(
                    200, content=b"protobuf-bytes",
                    request=httpx.Request("GET", url),
                )

        with patch.object(commute_client.httpx, "Client", _FakeClient):
            out = commute_client._fetch_nta_tripupdates("key")

        assert out == b"protobuf-bytes"
        assert len(calls) == 2, "should have retried exactly once"

    def test_fetch_raises_after_exhausting_attempts(self):
        from app.integrations.commute import client as commute_client

        class _AlwaysFails:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                raise httpx.ConnectError("CERTIFICATE_VERIFY_FAILED")

        with patch.object(commute_client.httpx, "Client", _AlwaysFails):
            with pytest.raises(httpx.ConnectError):
                commute_client._fetch_nta_tripupdates("key")


# ---------------------------------------------------------------------------
# db tier — sync_commute against real Postgres
# ---------------------------------------------------------------------------

def _fake_bus():
    return BusDeparture(
        trip_id="L2_0812", route="L2",
        depart=datetime(2026, 7, 13, 8, 12), arrive=datetime(2026, 7, 13, 8, 27),
        depart_confidence="live", arrive_confidence="live", delay_min=1.5,
    )


def _fake_dart():
    return DartService(
        traincode="E123", depart=datetime(2026, 7, 13, 8, 35),
        arrive=datetime(2026, 7, 13, 8, 55), destination="Howth",
    )


@pytest.mark.db
class TestSyncCommute:
    @pytest.fixture(autouse=True)
    def _patch_route_config(self, monkeypatch):
        """Routes come from config since 2026-07-28, and `routing` reads it
        through its own `plugin_config` reference — separate from the
        `sync.plugin_config` each test below stubs for feed/HA settings.

        Real-shaped values so these tests still exercise the actual
        `routes()` construction rather than a stubbed route object.
        """
        from types import SimpleNamespace

        monkeypatch.setattr(
            "app.integrations.commute.routing.plugin_config",
            lambda name: SimpleNamespace(
                commute_bus_home_stop="STOP_HOME",
                commute_bus_home_return_stop="STOP_HOME_RETURN",
                commute_bus_interchange_stop="STOP_INTERCHANGE",
                commute_rail_interchange_station="ICHG",
                commute_rail_city_station="DEST",
                commute_bus_routes=["L1", "L2"],
            ),
        )

    def test_records_decision_and_pushes_ha(self, db_session, monkeypatch):
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.integrations.commute.sync.plugin_config",
            lambda name: SimpleNamespace(ha_url="http://ha.local", ha_token="token")
            if name == "homeassistant"
            else SimpleNamespace(
                nta_api_key="test-key", commute_arrive_by="08:57",
                commute_interchange_buffer_min=6, commute_surface_decisions=True,
            ),
        )
        # Within max_feed_staleness_sec of the feed timestamps and before the
        # bus's dep_home (08:12) — otherwise solve() degrades on stale feeds
        # or the bus is no longer a *future* bus.
        monkeypatch.setattr(
            "app.integrations.commute.sync.dublin_now",
            lambda: datetime(2026, 7, 13, 8, 10, 30),
        )

        with (
            patch(
                "app.integrations.commute.sync.fetch_bus_departures",
                return_value=([_fake_bus()], datetime(2026, 7, 13, 8, 10)),
            ),
            patch(
                "app.integrations.commute.sync.fetch_dart_services",
                return_value=([_fake_dart()], datetime(2026, 7, 13, 8, 10)),
            ),
            patch("app.integrations.homeassistant.client.set_state", return_value=True),
        ):
            sync_commute(db_session)

        row = db_session.query(CommuteDecision).one()
        assert row.target_bus_trip_id == "L2_0812"
        assert row.target_train_code == "E123"
        assert row.bus_count == 1
        assert row.dart_count == 1
        assert row.ha_pushed is True

    def test_ha_down_still_commits_row(self, db_session, monkeypatch):
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.integrations.commute.sync.plugin_config",
            lambda name: SimpleNamespace(ha_url="", ha_token="") if name == "homeassistant"
            else SimpleNamespace(nta_api_key="test-key", commute_arrive_by="08:57", commute_interchange_buffer_min=6, commute_surface_decisions=True),
        )
        monkeypatch.setattr(
            "app.integrations.commute.sync.dublin_now",
            lambda: datetime(2026, 7, 13, 8, 10, 30),
        )

        with (
            patch(
                "app.integrations.commute.sync.fetch_bus_departures",
                return_value=([_fake_bus()], datetime(2026, 7, 13, 8, 10)),
            ),
            patch(
                "app.integrations.commute.sync.fetch_dart_services",
                return_value=([_fake_dart()], datetime(2026, 7, 13, 8, 10)),
            ),
        ):
            sync_commute(db_session)

        row = db_session.query(CommuteDecision).one()
        assert row.ha_pushed is False

    def test_feed_failure_still_records_degraded_decision(self, db_session, monkeypatch):
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.integrations.commute.sync.plugin_config",
            lambda name: SimpleNamespace(ha_url="", ha_token="") if name == "homeassistant"
            else SimpleNamespace(nta_api_key="test-key", commute_arrive_by="08:57", commute_interchange_buffer_min=6, commute_surface_decisions=True),
        )

        with (
            patch(
                "app.integrations.commute.sync.fetch_bus_departures",
                side_effect=RuntimeError("feed down"),
            ),
            patch(
                "app.integrations.commute.sync.fetch_dart_services",
                return_value=([_fake_dart()], datetime(2026, 7, 13, 8, 10)),
            ),
        ):
            sync_commute(db_session)

        row = db_session.query(CommuteDecision).one()
        assert row.state == "degraded"
        assert row.bus_count == 0

    def test_both_feeds_dead_raises_transient(self, db_session, monkeypatch):
        """Neither feed produced anything — nothing to solve on. No row
        committed, and a TransientError (network-y failure) propagates so
        the scheduler records the failure instead of silent "ok"."""
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.integrations.commute.sync.plugin_config",
            lambda name: SimpleNamespace(ha_url="", ha_token="") if name == "homeassistant"
            else SimpleNamespace(nta_api_key="test-key", commute_arrive_by="08:57", commute_interchange_buffer_min=6, commute_surface_decisions=True),
        )

        with (
            patch(
                "app.integrations.commute.sync.fetch_bus_departures",
                side_effect=httpx.ConnectError("connection refused"),
            ),
            patch(
                "app.integrations.commute.sync.fetch_dart_services",
                side_effect=httpx.TimeoutException("timed out"),
            ),
        ):
            with pytest.raises(TransientError):
                sync_commute(db_session)

        assert db_session.query(CommuteDecision).count() == 0

    def test_both_feeds_dead_auth_shaped_raises_permanent(self, db_session, monkeypatch):
        """Both feeds fail with an auth-shaped (401/403) status — a dead NTA
        key isn't fixed by retrying, so PermanentError should propagate."""
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.integrations.commute.sync.plugin_config",
            lambda name: SimpleNamespace(ha_url="", ha_token="") if name == "homeassistant"
            else SimpleNamespace(nta_api_key="test-key", commute_arrive_by="08:57", commute_interchange_buffer_min=6, commute_surface_decisions=True),
        )

        bad_response = httpx.Response(401, request=httpx.Request("GET", "https://example.test"))
        auth_error = httpx.HTTPStatusError("401", request=bad_response.request, response=bad_response)

        with (
            patch("app.integrations.commute.sync.fetch_bus_departures", side_effect=auth_error),
            patch("app.integrations.commute.sync.fetch_dart_services", side_effect=auth_error),
        ):
            with pytest.raises(PermanentError):
                sync_commute(db_session)

        assert db_session.query(CommuteDecision).count() == 0

    def test_one_feed_dead_no_raise_degraded_row_persisted(self, db_session, monkeypatch):
        """Restates the one-dead-feed tolerance explicitly against the new
        typed-error path: a single dead feed must NOT raise."""
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.integrations.commute.sync.plugin_config",
            lambda name: SimpleNamespace(ha_url="", ha_token="") if name == "homeassistant"
            else SimpleNamespace(nta_api_key="test-key", commute_arrive_by="08:57", commute_interchange_buffer_min=6, commute_surface_decisions=True),
        )

        with (
            patch(
                "app.integrations.commute.sync.fetch_bus_departures",
                side_effect=httpx.ConnectError("connection refused"),
            ),
            patch(
                "app.integrations.commute.sync.fetch_dart_services",
                return_value=([_fake_dart()], datetime(2026, 7, 13, 8, 10)),
            ),
        ):
            sync_commute(db_session)  # must not raise

        row = db_session.query(CommuteDecision).one()
        assert row.state == "degraded"


# ---------------------------------------------------------------------------
# unit tier — _push_ha_sensors partial-success semantics
# ---------------------------------------------------------------------------

class TestPushHaSensors:
    def test_primary_ok_secondary_fails_reports_pushed_true(self, monkeypatch):
        from app.integrations.commute.domain import Decision
        from app.integrations.commute.sync import _push_ha_sensors

        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.integrations.commute.sync.plugin_config",
            lambda name: SimpleNamespace(ha_url="http://ha.local", ha_token="token"),
        )

        decision = Decision(
            state="comfortable", status_text="Leave now", leave_in_min=5,
            confidence="live", degraded=False, reason="",
        )

        def fake_set_state(entity_id, *_a, **_kw):
            # Only the primary sensor succeeds.
            return entity_id == "sensor.commute_status"

        with patch("app.integrations.homeassistant.client.set_state", side_effect=fake_set_state):
            ok, failed = _push_ha_sensors(decision, datetime(2026, 7, 13, 8, 10))

        assert ok is True
        assert "sensor.commute_status" not in failed
        assert failed  # at least the secondaries failed

    def test_primary_fails_reports_pushed_false(self, monkeypatch):
        from app.integrations.commute.domain import Decision
        from app.integrations.commute.sync import _push_ha_sensors

        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.integrations.commute.sync.plugin_config",
            lambda name: SimpleNamespace(ha_url="http://ha.local", ha_token="token"),
        )

        decision = Decision(
            state="comfortable", status_text="Leave now", leave_in_min=5,
            confidence="live", degraded=False, reason="",
        )

        with patch("app.integrations.homeassistant.client.set_state", return_value=False):
            ok, failed = _push_ha_sensors(decision, datetime(2026, 7, 13, 8, 10))

        assert ok is False
        assert "sensor.commute_status" in failed


# ---------------------------------------------------------------------------
# unit tier — commute_query handler (mocked feeds, no DB)
# ---------------------------------------------------------------------------

def _outbound_bus():
    return BusDeparture(
        trip_id="L2_0812", route="L2",
        depart=datetime(2026, 7, 13, 8, 12), arrive=datetime(2026, 7, 13, 8, 27),
        depart_confidence="live", arrive_confidence="live", delay_min=1.0,
    )


def _outbound_dart():
    return DartService(
        traincode="E123", depart=datetime(2026, 7, 13, 8, 35),
        arrive=datetime(2026, 7, 13, 8, 55), destination="Howth",
    )


def _return_dart():
    return DartService(
        traincode="R1", depart=datetime(2026, 7, 13, 17, 20),
        arrive=datetime(2026, 7, 13, 17, 35), destination="Howth",
    )


def _return_bus():
    return BusDeparture(
        trip_id="L1_return", route="L1",
        depart=datetime(2026, 7, 13, 17, 45), arrive=datetime(2026, 7, 13, 18, 0),
        depart_confidence="live", arrive_confidence="live", delay_min=0.5,
    )


class TestHandleQuery:
    @pytest.fixture(autouse=True)
    def _patch_commute_config(self, monkeypatch):
        """Stub plugin_config for both readers — these are pure-unit tests
        (mock_session, no real DB), so stub rather than falling through to a
        real get_db().

        `routing` is patched too, and with real-shaped route keys, so these
        tests still exercise the actual `routes()` construction rather than a
        stubbed-out route dict. Route values moved from code constants to
        config on 2026-07-28.
        """
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            nta_api_key="test-key", commute_interchange_buffer_min=6,
            commute_surface_decisions=True, commute_arrive_by="08:57",
            commute_bus_home_stop="STOP_HOME",
            commute_bus_home_return_stop="STOP_HOME_RETURN",
            commute_bus_interchange_stop="STOP_INTERCHANGE",
            commute_rail_interchange_station="ICHG",
            commute_rail_city_station="DEST",
            commute_bus_routes=["L1", "L2"],
        )
        monkeypatch.setattr(
            "app.integrations.commute.tools.plugin_config", lambda name: cfg,
        )
        monkeypatch.setattr(
            "app.integrations.commute.routing.plugin_config", lambda name: cfg,
        )

    def test_outbound_arrive_by(self, mock_session):
        with (
            patch("app.integrations.commute.client.dublin_now", return_value=datetime(2026, 7, 13, 8, 0)),
            patch("app.integrations.commute.client.fetch_bus_departures", return_value=([_outbound_bus()], None)),
            patch("app.integrations.commute.client.fetch_dart_services", return_value=([_outbound_dart()], None)),
        ):
            out = json.loads(handle_query(mock_session, {"route": "outbound", "objective": "arrive_by", "time": "08:57"}))
        assert out["route"] == "outbound"
        assert out["bus_leg_configured"] is True
        assert out["target_bus"]["trip_id"] == "L2_0812"
        assert out["target_train"]["traincode"] == "E123"

    def test_outbound_depart_now_default(self, mock_session):
        with (
            patch("app.integrations.commute.client.dublin_now", return_value=datetime(2026, 7, 13, 8, 0)),
            patch("app.integrations.commute.client.fetch_bus_departures", return_value=([_outbound_bus()], None)),
            patch("app.integrations.commute.client.fetch_dart_services", return_value=([_outbound_dart()], None)),
        ):
            out = json.loads(handle_query(mock_session, {}))
        assert out["objective"] == "depart_now"
        assert out["state"] == "next_available"

    def test_return_depart_after(self, mock_session):
        with (
            patch("app.integrations.commute.client.dublin_now", return_value=datetime(2026, 7, 13, 17, 0)),
            patch("app.integrations.commute.client.fetch_bus_departures", return_value=([_return_bus()], None)),
            patch("app.integrations.commute.client.fetch_dart_services", return_value=([_return_dart()], None)),
        ):
            out = json.loads(handle_query(mock_session, {
                "route": "return", "objective": "depart_after", "time": "17:00",
            }))
        assert out["route"] == "return"
        assert out["target_train"]["traincode"] == "R1"
        assert out["target_bus"]["trip_id"] == "L1_return"

    def test_arrive_by_requires_time(self, mock_session):
        out = json.loads(handle_query(mock_session, {"objective": "arrive_by"}))
        assert "error" in out

    def test_unknown_route_errors(self, mock_session):
        out = json.loads(handle_query(mock_session, {"route": "sideways"}))
        assert "error" in out

    def test_unknown_objective_errors(self, mock_session):
        out = json.loads(handle_query(mock_session, {"objective": "teleport"}))
        assert "error" in out

    def test_dart_only_fallback_when_return_bus_leg_unconfigured(self, mock_session, monkeypatch):
        """If Route.bus_alight_stop were ever unset (e.g. a config rollback),
        commute_query still answers with the next DART, no bus leg.

        Routes are built from config now, so this patches the builder's output
        rather than a module-level ROUTES dict.
        """
        from app.integrations.commute import routing, tools as commute_tools

        real = routing.routes()
        patched = {
            **real,
            "return": dataclasses.replace(real["return"], bus_alight_stop=None),
        }
        monkeypatch.setattr(commute_tools, "routes", lambda: patched)

        with (
            patch("app.integrations.commute.client.dublin_now", return_value=datetime(2026, 7, 13, 17, 0)),
            patch("app.integrations.commute.client.fetch_dart_services", return_value=([_return_dart()], None)),
        ):
            out = json.loads(handle_query(mock_session, {"route": "return", "objective": "depart_now"}))
        assert out["bus_leg_configured"] is False
        assert out["target_bus"] is None
        assert out["target_train"]["traincode"] == "R1"
