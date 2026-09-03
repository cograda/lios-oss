"""F1 hardening (hardening-2026-08.md): the UI-token gate must fail CLOSED.

Before the fix, `check_ui_auth` in `app/main.py` only enforced the token
check when `settings.ui_token` was truthy (`... and settings.ui_token`) — an
unset/empty `HOME_UI_TOKEN` short-circuited the whole `if` and every
UI-token-gated `/api/*` route, including `DELETE /api/data/purge/{integration}`,
passed straight through unauthenticated.

These tests exercise the real `check_ui_auth` dispatch function from
`app.main` (not a re-implementation) mounted on a throwaway `FastAPI()` app
with two dummy routes — one under the gated `/api/` prefix, one under an
exempt prefix — so the behaviour under test is production code, but nothing
here touches a real database. Unit tier throughout.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.main import AUTH_EXEMPT_PREFIXES, check_ui_auth


def _build_app() -> FastAPI:
    """A minimal app carrying only the real `check_ui_auth` middleware."""
    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=check_ui_auth)

    @app.get("/api/protected")
    async def protected():
        return {"ok": True}

    # AUTH_EXEMPT_PREFIXES includes "/api/install/" — use that as the
    # deliberately-unauthenticated route rather than inventing a new prefix.
    assert "/api/install/" in AUTH_EXEMPT_PREFIXES
    exempt_path = "/api/install/testcode"

    @app.get(exempt_path)
    async def exempt():
        return {"ok": True}

    return app


def test_empty_ui_token_rejects_gated_route(monkeypatch):
    """F1: unset HOME_UI_TOKEN must reject, never pass through."""
    monkeypatch.setattr(settings, "ui_token", "")
    client = TestClient(_build_app())

    resp = client.get("/api/protected")

    assert resp.status_code == 503
    assert "HOME_UI_TOKEN not configured" in resp.text


def test_correct_token_via_header_passes(monkeypatch):
    monkeypatch.setattr(settings, "ui_token", "correct-token")
    client = TestClient(_build_app())

    resp = client.get("/api/protected", headers={"X-UI-Token": "correct-token"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_correct_token_via_cookie_passes(monkeypatch):
    monkeypatch.setattr(settings, "ui_token", "correct-token")
    client = TestClient(_build_app())
    client.cookies.set("ui_token", "correct-token")

    resp = client.get("/api/protected")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_wrong_token_rejected_with_401(monkeypatch):
    monkeypatch.setattr(settings, "ui_token", "correct-token")
    client = TestClient(_build_app())

    resp = client.get("/api/protected", headers={"X-UI-Token": "wrong-token"})

    assert resp.status_code == 401


def test_exempt_route_works_with_no_token_even_when_configured(monkeypatch):
    """Exempt-prefix routes (e.g. /api/install/*) must keep working
    regardless of the gate — install-code flow can't require a UI token."""
    monkeypatch.setattr(settings, "ui_token", "correct-token")
    client = TestClient(_build_app())

    resp = client.get("/api/install/testcode")

    assert resp.status_code == 200


def test_exempt_route_works_with_no_token_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "ui_token", "")
    client = TestClient(_build_app())

    resp = client.get("/api/install/testcode")

    assert resp.status_code == 200
