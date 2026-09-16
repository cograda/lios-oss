"""`POST /api/v1/alerts/events` auth (unit tier) — missing/wrong/right
bearer key, and unconfigured -> 503 (fail closed, distinct from a bad
credential). Mirrors `tests/test_signals_inlet_auth.py`'s shape.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.alerts import routes as alerts_routes

FIRING_ALERT = {
    "status": "firing",
    "labels": {"alertname": "HostDiskFull", "severity": "critical"},
    "annotations": {"summary": "Disk full"},
    "startsAt": "2026-09-14T08:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z",
    "fingerprint": "abc123",
}


class _FakeResult:
    rowcount = 1


class _FakeSession:
    def __init__(self):
        self.executed = []

    def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeResult()

    def commit(self):
        pass


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
    monkeypatch.setattr(alerts_routes, "get_db", lambda: fake_db)

    cfg = SimpleNamespace(alerts_inlet_key="s3cr3t")
    monkeypatch.setattr(alerts_routes._config_store, "plugin_config", lambda name: cfg)

    app = FastAPI()
    # Mirrors production mounting: the kernel router carries "/api",
    # `alerts.routes.router` carries "/v1/alerts" -> "/api/v1/alerts/events".
    app.include_router(alerts_routes.router, prefix="/api")
    client = TestClient(app, base_url="https://testserver")
    client._fake_session = fake_session
    return client


def test_missing_key_is_401(client):
    resp = client.post("/api/v1/alerts/events", json={"alerts": [FIRING_ALERT]})
    assert resp.status_code == 401


def test_wrong_key_is_401(client):
    resp = client.post(
        "/api/v1/alerts/events", json={"alerts": [FIRING_ALERT]},
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 401


def test_right_key_is_200(client):
    resp = client.post(
        "/api/v1/alerts/events", json={"alerts": [FIRING_ALERT]},
        headers={"Authorization": "Bearer s3cr3t"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["received"] == 1
    assert len(client._fake_session.executed) == 1


def test_non_bearer_authorization_header_is_401(client):
    resp = client.post(
        "/api/v1/alerts/events", json={"alerts": [FIRING_ALERT]},
        headers={"Authorization": "Basic s3cr3t"},
    )
    assert resp.status_code == 401


def test_no_configured_secret_is_503_fail_closed(monkeypatch, client):
    cfg = SimpleNamespace(alerts_inlet_key="")
    monkeypatch.setattr(alerts_routes._config_store, "plugin_config", lambda name: cfg)
    resp = client.post(
        "/api/v1/alerts/events", json={"alerts": [FIRING_ALERT]},
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 503


def test_no_configured_secret_is_503_even_with_no_header(monkeypatch, client):
    """Unconfigured must 503 before the missing-header 401 check ever
    matters — the point of the ordering in `_check_bearer`."""
    cfg = SimpleNamespace(alerts_inlet_key=None)
    monkeypatch.setattr(alerts_routes._config_store, "plugin_config", lambda name: cfg)
    resp = client.post("/api/v1/alerts/events", json={"alerts": [FIRING_ALERT]})
    assert resp.status_code == 503


def test_malformed_json_body_stores_nothing_but_still_200(client):
    resp = client.post(
        "/api/v1/alerts/events", content=b"not json",
        headers={"Authorization": "Bearer s3cr3t", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["received"] == 0
