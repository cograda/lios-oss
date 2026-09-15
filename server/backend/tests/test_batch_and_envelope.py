"""`POST /api/v1/batch` + the envelope + ETag/Cache-Control (unit tier).

Design: `vault/Projects/lios/Plans/2026-09-11 One data layer…md` §4.2/§5 step
1. This mirrors `tests/test_dispatch.py`'s unit-tier pattern — a fake
`get_db()`/mocked session stand in for Postgres, so none of this needs the
`db` marker's Postgres testcontainer — but drives the real `/api/v1/batch`
and `/api/v1/tools/{name}` FastAPI routes end to end via a tiny app that
mounts just `app.api.v1.router`, with `get_current_user` overridden to a
fixed authenticated user (no real bearer/DB round trip needed to prove the
routing/envelope/cap/timeout/scoping behaviour these tests are about).
"""

from __future__ import annotations

import contextlib
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.client_token import get_current_user
from app.models.users import User

pytestmark = pytest.mark.anyio


def _user(user_id: int = 1, name: str = "alex") -> User:
    return User(id=user_id, name=name, display_name=name.title())


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_db(mock_session):
    """A minimal `get_db()`-shaped stand-in around the shared mock_session
    (same shape as `tests/test_dispatch.py::fake_db`)."""

    class _FakeDb:
        def session(self):
            @contextlib.contextmanager
            def _cm():
                yield mock_session

            return _cm()

    return _FakeDb()


@pytest.fixture(autouse=True)
def _patch_dispatch_plumbing(monkeypatch, fake_db):
    """Same isolation `test_dispatch.py` uses: dispatch's own `get_db()` and
    the `runs` ledger write are faked so a real tool call through
    `dispatch_tool()` never touches Postgres."""
    import app.plugin.dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "get_db", lambda: fake_db)
    monkeypatch.setattr(dispatch_mod, "record_tool_call", lambda **kwargs: None)


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """Give each test its own tool_handlers/tool_metadata dicts."""
    from app.plugin import registry as registry_mod

    monkeypatch.setattr(registry_mod, "tool_handlers", dict(registry_mod.tool_handlers))
    monkeypatch.setattr(registry_mod, "tool_metadata", dict(registry_mod.tool_metadata))
    return registry_mod


def _register(registry_mod, name, handler, *, read_only=True, integration="probe"):
    registry_mod.tool_handlers[name] = (handler, integration)
    registry_mod.tool_metadata[name] = {"annotations": {"readOnlyHint": read_only}}


def _client(user: User | None = None) -> TestClient:
    from app.api import v1

    app = FastAPI()
    app.include_router(v1.router)
    app.dependency_overrides[get_current_user] = lambda: user or _user()
    return TestClient(app, base_url="https://testserver")


# ---------------------------------------------------------------------------
# Probe handlers
# ---------------------------------------------------------------------------

def _ok(payload):
    def h(session, arguments):
        return json.dumps(payload)
    return h


def _explode(session, arguments):
    raise ValueError("kaboom")


def _slow(seconds):
    def h(session, arguments):
        time.sleep(seconds)
        return json.dumps({"slept": seconds})
    return h


def _echo_current_user(session, arguments):
    from app.auth.context import current_user_id
    return json.dumps({"current_user_id": current_user_id()})


# ---------------------------------------------------------------------------
# Happy path + ordering
# ---------------------------------------------------------------------------

def test_batch_happy_path_in_order(registry):
    _register(registry, "probe_a", _ok({"v": "a"}))
    _register(registry, "probe_b", _ok({"v": "b"}))

    resp = _client().post("/api/v1/batch", json={"items": [
        {"id": "1", "tool": "probe_a", "args": {}},
        {"id": "2", "tool": "probe_b", "args": {}},
    ]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert [r["id"] for r in body["results"]] == ["1", "2"]
    assert body["results"][0] == {"id": "1", "ok": True, "result": {"v": "a"}}
    assert body["results"][1] == {"id": "2", "ok": True, "result": {"v": "b"}}


def test_batch_accepts_bare_list_body(registry):
    _register(registry, "probe_a", _ok({"v": "a"}))

    resp = _client().post("/api/v1/batch", json=[{"id": "x", "tool": "probe_a"}])

    assert resp.status_code == 200
    assert resp.json()["results"] == [{"id": "x", "ok": True, "result": {"v": "a"}}]


def test_batch_preserves_input_order_regardless_of_completion_order(registry):
    """Item 0 finishes last (it's the slow one); the response must still
    list it first — batch orders by input position, not by whoever
    finishes first."""
    _register(registry, "probe_slow", _slow(0.15))
    _register(registry, "probe_fast", _ok({"v": "fast"}))

    resp = _client().post("/api/v1/batch", json={"items": [
        {"id": "slow", "tool": "probe_slow"},
        {"id": "fast", "tool": "probe_fast"},
    ]})

    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()["results"]]
    assert ids == ["slow", "fast"]


# ---------------------------------------------------------------------------
# Per-item failure isolation
# ---------------------------------------------------------------------------

def test_batch_per_item_failure_never_fails_the_batch(registry):
    _register(registry, "probe_ok", _ok({"v": 1}))
    _register(registry, "probe_boom", _explode)

    resp = _client().post("/api/v1/batch", json={"items": [
        {"id": "good", "tool": "probe_ok"},
        {"id": "bad", "tool": "probe_boom"},
        {"id": "missing", "tool": "does_not_exist"},
    ]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True  # the batch itself succeeded

    by_id = {r["id"]: r for r in body["results"]}
    assert by_id["good"] == {"id": "good", "ok": True, "result": {"v": 1}}

    assert by_id["bad"]["ok"] is False
    assert by_id["bad"]["error"]["code"] == "internal"
    assert "kaboom" in by_id["bad"]["error"]["message"]
    assert by_id["bad"]["error"]["retryable"] is False

    assert by_id["missing"]["ok"] is False
    assert by_id["missing"]["error"]["code"] == "unknown_tool"


# ---------------------------------------------------------------------------
# Cap
# ---------------------------------------------------------------------------

def test_batch_rejects_over_cap_with_400(registry, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "batch_max_items", 2)
    _register(registry, "probe_a", _ok({"v": "a"}))

    resp = _client().post("/api/v1/batch", json={"items": [
        {"tool": "probe_a"}, {"tool": "probe_a"}, {"tool": "probe_a"},
    ]})

    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_args"


def test_batch_rejects_malformed_body_with_400():
    resp = _client().post("/api/v1/batch", json={"not_items": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_args"


def test_batch_empty_list_is_a_clean_no_op(registry):
    resp = _client().post("/api/v1/batch", json={"items": []})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "results": []}


# ---------------------------------------------------------------------------
# Timeout budget
# ---------------------------------------------------------------------------

def test_batch_timeout_budget_reports_per_item_timeout(registry, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "batch_timeout_seconds", 0.05)
    _register(registry, "probe_slow", _slow(0.3))
    _register(registry, "probe_fast", _ok({"v": "fast"}))

    start = time.monotonic()
    resp = _client().post("/api/v1/batch", json={"items": [
        {"id": "slow", "tool": "probe_slow"},
        {"id": "fast", "tool": "probe_fast"},
    ]})
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    # The response is bounded by the slow item's own sleep (cancelling an
    # `asyncio.to_thread` future can't interrupt the OS thread already
    # blocked in `time.sleep`), NOT by the item running to its full 60s
    # per-tool timeout — this is the "budget bounds the request" contract,
    # not proof the underlying thread stopped instantly.
    assert elapsed < 1.5
    body = resp.json()
    by_id = {r["id"]: r for r in body["results"]}
    assert by_id["fast"]["ok"] is True
    assert by_id["slow"]["ok"] is False
    assert by_id["slow"]["error"]["code"] == "timeout"
    assert by_id["slow"]["error"]["retryable"] is True


# ---------------------------------------------------------------------------
# Auth scoping — a batch cannot widen the owner
# ---------------------------------------------------------------------------

def test_batch_item_runs_as_the_authenticated_user_regardless_of_args(registry):
    _register(registry, "probe_whoami", _echo_current_user)

    resp = _client(_user(user_id=7, name="sam")).post("/api/v1/batch", json={"items": [
        # Even if a caller tries to smuggle a different owner into args, the
        # dispatched call runs as the authenticated user — the same
        # scoping a lone `POST /tools/probe_whoami` call would get.
        {"id": "x", "tool": "probe_whoami", "args": {"user_id": 999, "owner": "alex"}},
    ]})

    assert resp.status_code == 200
    result = resp.json()["results"][0]["result"]
    assert result["current_user_id"] == 7


def test_batch_item_capability_refusal_is_forbidden_code(registry):
    """A read-only-scoped bearer calling a non-read-only tool through batch
    gets the same refusal `dispatch_tool` gives a lone call — surfaced as
    its own per-item error, not a batch failure."""
    _register(registry, "probe_write", _ok({"v": 1}), read_only=False)

    user = _user()
    user.client_token_scope = "readonly"

    resp = _client(user).post("/api/v1/batch", json={"items": [
        {"id": "w", "tool": "probe_write"},
    ]})

    assert resp.status_code == 200
    entry = resp.json()["results"][0]
    assert entry["ok"] is False
    assert entry["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Envelope on /tools/{name} — opt-in only
# ---------------------------------------------------------------------------

def test_tools_route_default_shape_is_unchanged(registry):
    """No X-Lios-Envelope header: the deployed-consumer shape (string
    `error`, no `warnings` key) must not move."""
    _register(registry, "probe_boom", _explode)

    resp = _client().post("/api/v1/tools/probe_boom", json={})
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert isinstance(body["error"], str)
    assert "kaboom" in body["error"]


def test_tools_route_envelope_opt_in_error_shape(registry):
    _register(registry, "probe_boom", _explode)

    resp = _client().post(
        "/api/v1/tools/probe_boom", json={}, headers={"X-Lios-Envelope": "1"},
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert isinstance(body["error"], dict)
    assert body["error"]["code"] == "internal"
    assert "kaboom" in body["error"]["message"]
    assert body["error"]["retryable"] is False


def test_tools_route_envelope_via_accept_header(registry):
    _register(registry, "probe_ok", _ok({"v": 1}))
    resp = _client().post(
        "/api/v1/tools/probe_ok", json={},
        headers={"Accept": "application/vnd.lios.envelope+json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "result": {"v": 1}}


def test_tools_route_envelope_no_longer_folds_render_skipped_into_warnings(registry):
    """The tasks tools' `render_skipped`/`render_error` pattern was removed
    2026-09-14 (Task Backlog.md's render became unconditional, so it never
    reports a skipped render any more) — and with it, the envelope's only
    populator of `warnings`. A result dict that happens to carry those keys
    (e.g. a stale caller, or an old snapshot) is no longer folded into a
    `warnings` array; it passes through on `result` untouched, like any
    other field the envelope doesn't recognise."""
    _register(registry, "probe_render", _ok({
        "created": {"uid": "TASK-1"}, "render_skipped": True, "render_error": "vault write failed",
    }))

    resp = _client().post(
        "/api/v1/tools/probe_render", json={}, headers={"X-Lios-Envelope": "1"},
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["result"]["render_skipped"] is True
    assert "warnings" not in body


def test_batch_item_no_longer_surfaces_render_warnings(registry):
    _register(registry, "probe_render", _ok({
        "created": {"uid": "TASK-1"}, "render_skipped": True, "render_error": "vault write failed",
    }))

    resp = _client().post("/api/v1/batch", json={"items": [{"id": "t", "tool": "probe_render"}]})
    entry = resp.json()["results"][0]
    assert entry["ok"] is True
    assert "warnings" not in entry


# ---------------------------------------------------------------------------
# ETag / Cache-Control (readOnlyHint tools only)
# ---------------------------------------------------------------------------

def test_readonly_tool_gets_etag_and_304_on_if_none_match(registry):
    _register(registry, "probe_read", _ok({"v": "stable"}), read_only=True)

    client = _client()
    first = client.post("/api/v1/tools/probe_read", json={})
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert etag

    second = client.post(
        "/api/v1/tools/probe_read", json={}, headers={"If-None-Match": etag},
    )
    assert second.status_code == 304
    assert second.headers["etag"] == etag


def test_write_tool_gets_no_etag(registry):
    _register(registry, "probe_write", _ok({"v": 1}), read_only=False)

    resp = _client().post("/api/v1/tools/probe_write", json={})
    assert resp.status_code == 200
    assert "etag" not in {k.lower() for k in resp.headers.keys()}


def test_freshness_hints_drive_cache_control(registry):
    from app.plugin import registry as registry_mod

    # Real registered names pick up the real per-tool freshness table.
    registry_mod.tool_handlers["weather_current"] = (_ok({"v": "sun"}), "weather")
    registry_mod.tool_metadata["weather_current"] = {"annotations": {"readOnlyHint": True}}
    registry_mod.tool_handlers["tasks_query"] = (_ok({"v": []}), "tasks")
    registry_mod.tool_metadata["tasks_query"] = {"annotations": {"readOnlyHint": True}}
    _register(registry_mod, "probe_read_unlisted", _ok({"v": 1}), read_only=True)

    client = _client()
    weather = client.post("/api/v1/tools/weather_current", json={})
    tasks = client.post("/api/v1/tools/tasks_query", json={})
    unlisted = client.post("/api/v1/tools/probe_read_unlisted", json={})

    assert weather.headers["cache-control"] == "private, max-age=600"
    assert tasks.headers["cache-control"] == "no-store"
    assert unlisted.headers["cache-control"] == "no-store"


def _real_app_client(user: User | None = None) -> TestClient:
    """Drive the *real* app (`app.main:app`), middleware stack and all.

    `_client()` above mounts only `v1.router` on a bare `FastAPI()`, which is
    why `test_freshness_hints_drive_cache_control` never exercised
    `api_security_headers` (main.py) clobbering the route's own
    `Cache-Control` with its blanket `no-store` default (PR #214 landed the
    per-tool hint; the middleware was still assigning over it in
    production). `/api/v1/` is in `AUTH_EXEMPT_PREFIXES`, so the dashboard
    session-gating middleware passes this straight through — only
    `get_current_user` needs overriding, same as `_client()`.
    """
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: user or _user()
    return TestClient(app, base_url="https://testserver")


def test_middleware_does_not_clobber_route_level_cache_control(registry):
    """The blanket `api_security_headers` middleware must defer to a route
    that already set its own `Cache-Control` (private, max-age=N), while
    still defaulting unlisted/uncacheable tools and non-tools routes to
    `no-store` — the failure this guards was silent: `test_
    freshness_hints_drive_cache_control` passed throughout because it never
    ran through the real middleware stack."""
    from app.plugin import registry as registry_mod

    registry_mod.tool_handlers["weather_current"] = (_ok({"v": "sun"}), "weather")
    registry_mod.tool_metadata["weather_current"] = {"annotations": {"readOnlyHint": True}}
    _register(registry_mod, "probe_read_unlisted", _ok({"v": 1}), read_only=True)

    from app.main import app as real_app

    client = _real_app_client()
    try:
        weather = client.post("/api/v1/tools/weather_current", json={})
        assert weather.headers["cache-control"] == "private, max-age=600"
        assert weather.headers["x-content-type-options"] == "nosniff"

        unlisted = client.post("/api/v1/tools/probe_read_unlisted", json={})
        assert unlisted.headers["cache-control"] == "no-store"

        # A plain (non-tools) /api/ route that sets nothing of its own still
        # gets the middleware's default.
        health = client.get("/api/health")
        assert health.headers["cache-control"] == "no-store"
    finally:
        # `app.main.app` is a process-wide singleton; clear the override so
        # it doesn't leak into a later test that imports the real app.
        real_app.dependency_overrides.pop(get_current_user, None)
