"""Tests for F8 — the per-IP auth-boundary rate limiter.

Failure-budget semantics: only FAILED auth attempts (401s) spend budget.
Every MCP tool call authenticates, and the household's normal workflow fans
parallel subagents out over the MCP tools — legitimate traffic at any volume
must never 429 itself. `is_over_limit()` is checked at the three auth entry
points before any DB work; `record_failure()` is spent on 401 paths (via the
`record_auth_event` chokepoint for the bearer paths, explicitly for the
dashboard sign-in route).

`app.auth.rate_limit` is pure stdlib (time + a bounded dict), so it's tested
directly rather than through the FastAPI app; wiring tests for the three F8
call sites follow below.
"""

from __future__ import annotations

import pytest

from app.auth import rate_limit


@pytest.fixture(autouse=True)
def _clean_limiter():
    """Every test gets a fresh limiter — global module state otherwise leaks
    between tests (and between this file and whatever exercises the real
    auth paths elsewhere)."""
    rate_limit.reset()
    yield
    rate_limit.reset()


def _exhaust(ip: str, now: float | None = None) -> None:
    for i in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW):
        rate_limit.record_failure(ip, now=None if now is None else now + i * 0.01)


class TestFailureBudget:
    def test_checking_never_spends_budget(self):
        """THE property the failures-only design exists for: any volume of
        successful traffic (which only ever checks) stays under the limit."""
        for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW * 10):
            assert rate_limit.is_over_limit("1.2.3.4") is False

    def test_under_threshold_failures_do_not_block(self):
        for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW - 1):
            rate_limit.record_failure("1.2.3.4")
        assert rate_limit.is_over_limit("1.2.3.4") is False

    def test_blocks_at_the_threshold(self):
        _exhaust("1.2.3.4")
        assert rate_limit.is_over_limit("1.2.3.4") is True

    def test_different_ips_have_independent_budgets(self):
        _exhaust("1.2.3.4")
        assert rate_limit.is_over_limit("1.2.3.4") is True
        assert rate_limit.is_over_limit("5.6.7.8") is False

    def test_window_slides_forward(self):
        """Failures outside the window no longer count against the budget."""
        base = 1000.0
        _exhaust("1.2.3.4", now=base)
        assert rate_limit.is_over_limit("1.2.3.4", now=base + 0.5) is True

        later = base + rate_limit.WINDOW_SECONDS + 1
        assert rate_limit.is_over_limit("1.2.3.4", now=later) is False

    def test_reset_clears_all_state(self):
        _exhaust("1.2.3.4")
        assert rate_limit.is_over_limit("1.2.3.4") is True
        rate_limit.reset()
        assert rate_limit.is_over_limit("1.2.3.4") is False


class TestAuthEventHook:
    """`record_auth_event(outcome="401")` is the one chokepoint every
    bearer-path failure funnels through — it must spend budget, and other
    outcomes must not."""

    @pytest.fixture(autouse=True)
    def _no_db(self, monkeypatch):
        """The audit insert is best-effort (never raises); the budget spend
        must happen even when it fails. Making get_db blow up pins both."""
        def _boom():
            raise RuntimeError("no db in unit tier")

        monkeypatch.setattr("app.services.auth_events.get_db", _boom)

    def test_401_event_spends_budget(self):
        from app.services.auth_events import record_auth_event

        for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW):
            record_auth_event(outcome="401", source_ip="7.7.7.7", transport="http")
        assert rate_limit.is_over_limit("7.7.7.7") is True

    def test_non_401_event_spends_nothing(self):
        from app.services.auth_events import record_auth_event

        for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW):
            record_auth_event(outcome="issued", source_ip="7.7.7.7", transport="http")
        assert rate_limit.is_over_limit("7.7.7.7") is False


class TestClientTokenWiring:
    """`get_current_user` (the V3/HTTP bearer path) must consult the limiter
    before any DB lookup or `auth_events` write."""

    def test_over_limit_ip_gets_429_not_401(self, monkeypatch):
        from fastapi import HTTPException

        from app.auth import client_token

        _exhaust("9.9.9.9")

        events = []
        monkeypatch.setattr(
            "app.services.auth_events.record_auth_event",
            lambda **kw: events.append(kw),
        )

        class _FakeClient:
            host = "9.9.9.9"

        class _FakeRequest:
            client = _FakeClient()

        with pytest.raises(HTTPException) as excinfo:
            client_token.get_current_user(authorization="", request=_FakeRequest())

        assert excinfo.value.status_code == 429
        # The whole point: no auth_events write for the rate-limited request.
        assert events == []

    def test_under_limit_ip_still_gets_normal_401(self, monkeypatch):
        from fastapi import HTTPException

        from app.auth import client_token

        events = []
        monkeypatch.setattr(
            "app.services.auth_events.record_auth_event",
            lambda **kw: events.append(kw),
        )

        class _FakeClient:
            host = "1.1.1.1"

        class _FakeRequest:
            client = _FakeClient()

        with pytest.raises(HTTPException) as excinfo:
            client_token.get_current_user(authorization="", request=_FakeRequest())

        assert excinfo.value.status_code == 401
        assert len(events) == 1


class TestMcpServerWiring:
    """`_authenticate_request` (the MCP bearer path) raises `RateLimited`
    rather than returning None, so `mcp_asgi_app` can 429 instead of 401."""

    def test_over_limit_raises_rate_limited(self, monkeypatch):
        from app.mcp import server as mcp_server

        _exhaust("8.8.8.8")

        monkeypatch.setattr(
            "app.services.auth_events.record_auth_event",
            lambda **kw: None,
        )

        class _FakeClient:
            host = "8.8.8.8"

        class _FakeRequest:
            client = _FakeClient()
            headers = {}

        with pytest.raises(mcp_server.RateLimited):
            mcp_server._authenticate_request(_FakeRequest())

    def test_under_limit_returns_none_for_missing_token(self, monkeypatch):
        from app.mcp import server as mcp_server

        monkeypatch.setattr(
            "app.services.auth_events.record_auth_event",
            lambda **kw: None,
        )

        class _FakeClient:
            host = "2.2.2.2"

        class _FakeRequest:
            client = _FakeClient()
            headers = {}

        assert mcp_server._authenticate_request(_FakeRequest()) is None


class TestUiLoginWiring:
    """`POST /auth/login` also sits on the limiter — same brute-forceable
    shared-secret shape as the bearer paths, but it doesn't write auth
    events, so it records its own failures explicitly.

    Mounted on a throwaway FastAPI app (same pattern as
    `test_ui_auth_middleware.py`) so this exercises the real `login` route
    function, via a real request/response cycle, without booting the full app.
    """

    def _build_app(self):
        from fastapi import FastAPI

        from app.routes.auth import router

        app = FastAPI()
        app.include_router(router, prefix="/api")
        return app

    def _wire(self, monkeypatch):
        """`login` resolves the posted bearer with `resolve_token_to_user`
        and opens a session with `create_session` — both stubbed here so the
        budget accounting is tested without a database."""
        from types import SimpleNamespace

        from app.routes import auth as auth_routes

        def _resolve(token):
            if token != "the-real-token":
                return None
            user = SimpleNamespace(id=1, name="alex", display_name="Alex", is_admin=True)
            user.client_token_id = 7
            return user

        monkeypatch.setattr(auth_routes, "resolve_token_to_user", _resolve)
        monkeypatch.setattr(auth_routes, "create_session", lambda user: "session-id")
        monkeypatch.setattr(
            "app.services.auth_events.record_auth_event", lambda **kw: None,
        )

    def test_under_limit_never_429s(self, monkeypatch):
        from fastapi.testclient import TestClient

        self._wire(monkeypatch)
        client = TestClient(self._build_app())
        response = client.post("/api/auth/login", json={"token": "wrong"})
        assert response.status_code == 401

    def test_exhausting_failed_guesses_yields_429(self, monkeypatch):
        """TestClient always reports a fixed client IP, so repeated WRONG
        tokens from one TestClient exhaust that identity's failure budget."""
        from fastapi.testclient import TestClient

        self._wire(monkeypatch)
        client = TestClient(self._build_app())
        last = None
        for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW + 1):
            last = client.post("/api/auth/login", json={"token": "wrong"})
        assert last.status_code == 429

    def test_correct_logins_never_burn_budget(self, monkeypatch):
        """A valid caller logging in repeatedly must never be limited —
        the failures-only property, end to end through the route."""
        from fastapi.testclient import TestClient

        self._wire(monkeypatch)
        client = TestClient(self._build_app())
        for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW + 5):
            response = client.post("/api/auth/login", json={"token": "the-real-token"})
            assert response.status_code == 200
