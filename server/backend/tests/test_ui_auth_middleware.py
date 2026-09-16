"""The dashboard session gate (2026-09-06 — one credential: the per-user bearer).

`check_ui_auth` in `app/main.py` used to compare a cookie/header against the
shared `HOME_UI_TOKEN`. It now resolves the `lios_session` cookie to a person
and runs the request inside `use_user(user.id)`, so `current_user_id()` is
bound for every handler downstream — the same binding an MCP call gets.

These tests exercise the real `check_ui_auth` dispatch function mounted on a
throwaway `FastAPI()` app. `resolve_session` is replaced by a stub so nothing
here needs Postgres; `tests/test_ui_session.py` covers the real resolver.
Two routes read the ContextVar — one `async def`, one plain `def` (which
FastAPI runs in a worker thread) — because the whole reason the gate is a
middleware rather than a dependency is that a ContextVar set in a sync
dependency is not guaranteed visible to the endpoint
(`app/auth/client_token.py`'s module docstring). Both shapes must see it.
"""

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import app.auth.ui_session as ui_session
from app.auth.context import current_user_id, current_user_id_or_none
from app.auth.ui_session import SESSION_COOKIE, current_ui_user, require_admin
from app.main import AUTH_EXEMPT_PREFIXES, check_ui_auth


def _user(uid: int, *, admin: bool = False):
    u = SimpleNamespace(id=uid, name=f"user{uid}", display_name=f"User {uid}", is_admin=admin)
    u.client_token_id = 100 + uid
    return u


@pytest.fixture
def sessions(monkeypatch):
    """cookie value → user. Anything not in the dict resolves to None."""
    table: dict[str, object] = {}
    monkeypatch.setattr(ui_session, "resolve_session", lambda value: table.get(value or ""))
    return table


def _build_app() -> FastAPI:
    """A minimal app carrying only the real `check_ui_auth` middleware."""
    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=check_ui_auth)

    @app.get("/api/whoami-async")
    async def whoami_async():
        return {"user_id": current_user_id()}

    @app.get("/api/whoami-sync")
    def whoami_sync():  # plain def → FastAPI runs it in the threadpool
        return {"user_id": current_user_id()}

    @app.get("/api/admin-only")
    async def admin_only(user=Depends(require_admin)):
        return {"admin": user.name}

    @app.get("/api/me")
    async def me(user=Depends(current_ui_user)):
        return {"name": user.name}

    # AUTH_EXEMPT_PREFIXES includes "/api/install/" — use that as the
    # deliberately-unauthenticated route rather than inventing a new prefix.
    assert "/api/install/" in AUTH_EXEMPT_PREFIXES

    @app.get("/api/install/testcode")
    async def exempt():
        return {"ok": True, "bound": current_user_id_or_none()}

    return app


def test_no_cookie_is_401(sessions):
    client = TestClient(_build_app())
    resp = client.get("/api/whoami-async")
    assert resp.status_code == 401


def test_unknown_or_revoked_session_is_401(sessions):
    client = TestClient(_build_app())
    client.cookies.set(SESSION_COOKIE, "not-a-live-session")
    assert client.get("/api/whoami-async").status_code == 401


def test_no_shared_secret_is_accepted(sessions):
    """The old `X-UI-Token` header and `ui_token` cookie must do nothing —
    there is no shared value they could match any more."""
    client = TestClient(_build_app())
    client.cookies.set("ui_token", "anything")
    assert client.get("/api/whoami-async", headers={"X-UI-Token": "anything"}).status_code == 401


def test_session_binds_the_user_for_an_async_route(sessions):
    sessions["sess-2"] = _user(2)
    client = TestClient(_build_app())
    client.cookies.set(SESSION_COOKIE, "sess-2")
    resp = client.get("/api/whoami-async")
    assert resp.status_code == 200
    assert resp.json() == {"user_id": 2}


def test_session_binds_the_user_for_a_sync_route(sessions):
    """The threadpool copy: a `def` endpoint must see the same binding."""
    sessions["sess-2"] = _user(2)
    client = TestClient(_build_app())
    client.cookies.set(SESSION_COOKIE, "sess-2")
    resp = client.get("/api/whoami-sync")
    assert resp.status_code == 200
    assert resp.json() == {"user_id": 2}


def test_binding_does_not_leak_between_requests(sessions):
    sessions["sess-1"] = _user(1)
    sessions["sess-2"] = _user(2)
    app = _build_app()
    a, b = TestClient(app), TestClient(app)
    a.cookies.set(SESSION_COOKIE, "sess-1")
    b.cookies.set(SESSION_COOKIE, "sess-2")
    assert a.get("/api/whoami-sync").json() == {"user_id": 1}
    assert b.get("/api/whoami-sync").json() == {"user_id": 2}
    assert a.get("/api/whoami-async").json() == {"user_id": 1}
    # After the requests, nothing is left bound on the test's own context.
    assert current_user_id_or_none() in (None, 1)  # 1 = conftest's autouse pin


def test_current_ui_user_and_require_admin(sessions):
    sessions["member"] = _user(2)
    sessions["boss"] = _user(1, admin=True)
    app = _build_app()

    member = TestClient(app)
    member.cookies.set(SESSION_COOKIE, "member")
    assert member.get("/api/me").json() == {"name": "user2"}
    assert member.get("/api/admin-only").status_code == 403

    boss = TestClient(app)
    boss.cookies.set(SESSION_COOKIE, "boss")
    assert boss.get("/api/admin-only").status_code == 200


def test_exempt_route_works_with_no_session_and_binds_nobody(sessions):
    """Exempt-prefix routes (e.g. /api/install/*) must keep working — the
    install-code flow can't require a session — and the gate must not
    invent a user for them."""
    client = TestClient(_build_app())
    resp = client.get("/api/install/testcode")
    assert resp.status_code == 200
    # conftest pins user 1 on the test's own context; the middleware itself
    # bound nothing extra for an exempt route.
    assert resp.json()["bound"] in (None, 1)
