"""`POST /api/v1/signals/{source}` auth (unit tier) — 401 wrong/missing key,
200 right key via query param OR header. DB and watcher dispatch are faked
so this stays a unit-tier test of the auth gate itself.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.signals import routes as signals_routes

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for i, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = i

    def commit(self):
        pass

    def get(self, model, id_):
        for obj in self.added:
            if getattr(obj, "id", None) == id_:
                return obj
        return None


class _FakeDb:
    def __init__(self, session):
        self._session = session

    def session(self):
        @contextlib.contextmanager
        def _cm():
            yield self._session

        return _cm()


@pytest.fixture
def client(monkeypatch):
    fake_session = _FakeSession()
    fake_db = _FakeDb(fake_session)
    monkeypatch.setattr(signals_routes, "get_db", lambda: fake_db)

    cfg = SimpleNamespace(signals_protect_key="s3cr3t", signals_devices={})
    monkeypatch.setattr(signals_routes._config_store, "plugin_config", lambda name: cfg)
    # No watcher matches an unmapped device on an unregistered "source" —
    # avoids dragging real watcher/vision machinery into an auth test.
    monkeypatch.setattr(
        "app.integrations.signals.watchers.registry.WATCHERS", [],
    )

    app = FastAPI()
    # Mirrors production mounting: `app/routes/__init__.py`'s kernel router
    # carries "/api", and `signals.routes.router` itself carries "/v1/signals"
    # -> "/api/v1/signals/{source}".
    app.include_router(signals_routes.router, prefix="/api")
    return TestClient(app, base_url="https://testserver")


def test_missing_key_is_401(client):
    resp = client.post("/api/v1/signals/protect", json={})
    assert resp.status_code == 401


def test_wrong_key_is_401_via_query(client):
    resp = client.post("/api/v1/signals/protect?key=nope", json={})
    assert resp.status_code == 401


def test_wrong_key_is_401_via_header(client):
    resp = client.post("/api/v1/signals/protect", json={}, headers={"X-Signal-Key": "nope"})
    assert resp.status_code == 401


def test_right_key_via_query_is_200(client):
    resp = client.post("/api/v1/signals/protect?key=s3cr3t", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_right_key_via_header_is_200(client):
    resp = client.post("/api/v1/signals/protect", json={}, headers={"X-Signal-Key": "s3cr3t"})
    assert resp.status_code == 200


def test_no_configured_secret_is_401_fail_closed(monkeypatch, client):
    cfg = SimpleNamespace(signals_protect_key="", signals_devices={})
    monkeypatch.setattr(signals_routes._config_store, "plugin_config", lambda name: cfg)
    resp = client.post("/api/v1/signals/protect?key=anything", json={})
    assert resp.status_code == 401


def test_unrecognised_shape_stores_as_unknown_kind(client):
    resp = client.post("/api/v1/signals/protect?key=s3cr3t", json={"garbage": True})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "unknown"


# ---------------------------------------------------------------------------
# Device-key normalisation + sources fallback (added 2026-09-11, following a
# real Protect "Test Alarm": MACs arrive UPPERCASE WITHOUT COLONS, and the
# alarm's trigger device is a placeholder while `sources[]` carries the
# real camera.
# ---------------------------------------------------------------------------

TEST_ALARM_BODY = {
    "alarm": {
        "name": "Milkman",
        "sources": [{"type": "include", "device": "A89C6CB03B50"}],
        "triggers": [{"key": "person", "device": "FAKE_MAC",
                      "eventId": "testEventId", "timestamp": 1789162652753}],
        "eventPath": "/protect/events/event/testEventId",
        "conditions": [{"condition": {"type": "is", "source": "person"}}],
        "eventLocalLink": "https://192.168.1.1/protect/events/event/testEventId",
    },
    "timestamp": 1789162652755,
}


def test_config_devices_colon_form_matches_colonless_payload(monkeypatch, client):
    """A `signals_devices` entry written colon-form (as copied from HA's
    device registry) must still resolve a payload MAC that Protect actually
    sends colonless — this is the bug the 2026-09-11 Test Alarm surfaced."""
    cfg = SimpleNamespace(
        signals_protect_key="s3cr3t",
        signals_devices={"8c:ed:e1:72:f4:13": "front_door"},
    )
    monkeypatch.setattr(signals_routes._config_store, "plugin_config", lambda name: cfg)

    resp = client.post(
        "/api/v1/signals/protect?key=s3cr3t",
        json={"alarm": {"triggers": [{"key": "person", "device": "8CEDE172F413",
                                       "timestamp": 1757600000000}]}},
    )
    assert resp.status_code == 200
    assert resp.json()["device_name"] == "front_door"


def test_sources_fallback_resolves_name_when_trigger_device_is_unknown(monkeypatch, client):
    """The real Test Alarm shape: trigger device is a placeholder unknown to
    config, but `sources[]` names exactly one known device — the response
    (and stored event) resolve `device_name` from it, marking the stored
    payload `resolved_via: "sources"` while leaving the raw trigger value in
    `device_key`."""
    cfg = SimpleNamespace(
        signals_protect_key="s3cr3t",
        signals_devices={"a8:9c:6c:b0:3b:50": "front_door"},
    )
    monkeypatch.setattr(signals_routes._config_store, "plugin_config", lambda name: cfg)

    resp = client.post("/api/v1/signals/protect?key=s3cr3t", json=TEST_ALARM_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_name"] == "front_door"

    from app.integrations.signals.models import SignalEvent

    stored = signals_routes.get_db()._session.added[0]
    assert isinstance(stored, SignalEvent)
    assert stored.device_key == "fake_mac"  # raw trigger value, not overwritten
    assert stored.device_name == "front_door"
    assert stored.payload["_signals_resolved_via"] == "sources"


def test_sources_fallback_does_not_apply_with_multiple_known_sources(monkeypatch, client):
    """Two known sources is ambiguous — no fallback, no guess."""
    cfg = SimpleNamespace(
        signals_protect_key="s3cr3t",
        signals_devices={"a8:9c:6c:b0:3b:50": "front_door", "aa:bb:cc:dd:ee:ff": "side_gate"},
    )
    monkeypatch.setattr(signals_routes._config_store, "plugin_config", lambda name: cfg)

    body = {
        "alarm": {
            "sources": [
                {"type": "include", "device": "A89C6CB03B50"},
                {"type": "include", "device": "AA:BB:CC:DD:EE:FF"},
            ],
            "triggers": [{"key": "person", "device": "FAKE_MAC", "timestamp": 1757600000000}],
        },
    }
    resp = client.post("/api/v1/signals/protect?key=s3cr3t", json=body)
    assert resp.status_code == 200
    assert resp.json()["device_name"] is None

    stored = signals_routes.get_db()._session.added[0]
    assert "_signals_resolved_via" not in stored.payload


def test_no_sources_fallback_marker_when_trigger_device_already_known(monkeypatch, client):
    """The ordinary case (trigger device itself is known) must not pick up
    a spurious `resolved_via` marker."""
    cfg = SimpleNamespace(
        signals_protect_key="s3cr3t",
        signals_devices={"8c:ed:e1:72:f4:13": "front_door"},
    )
    monkeypatch.setattr(signals_routes._config_store, "plugin_config", lambda name: cfg)

    resp = client.post(
        "/api/v1/signals/protect?key=s3cr3t",
        json={"alarm": {"triggers": [{"key": "person", "device": "8C:ED:E1:72:F4:13",
                                       "timestamp": 1757600000000}]}},
    )
    assert resp.status_code == 200
    assert resp.json()["device_name"] == "front_door"

    stored = signals_routes.get_db()._session.added[0]
    assert "_signals_resolved_via" not in stored.payload
