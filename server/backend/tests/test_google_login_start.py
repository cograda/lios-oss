"""`GET /api/auth/google/login` requires a signed, short-lived `start` (2026-09-07).

The route is exempt from the dashboard session for a real reason (the re-auth
link is followed on the Tailscale hostname, where the `comar.lab` cookie is
not sent — see `AUTH_EXEMPT` in app/main.py). Exempt used to mean anyone on
the tailnet could START a Google grant naming any user; only the callback's
HMAC `state` protected the completion. Now the start leg carries its own
proof: `start` = HMAC over (account, user, expiry), minted only by
session-authenticated routes (`/api/auth/google/login-url`, the dashboard
summary) or the bearer-run `system_alerts` tool.

Unit tier: the DB is stubbed, `create_auth_url` is real (so a valid start
really does redirect to Google), no network.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import oauth
from app.auth.ui_session import current_ui_user
from app.config import settings
from app.routes import auth as auth_routes
from app.routes.auth import router as auth_router

ACCOUNT = "someone@example.com"


class _Sess:
    """A session whose `query(User).filter_by(name=...).first()` finds any
    of the two seeded household names and nobody else."""

    KNOWN = {"alex", "sam"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, _model):
        sess = self

        class _Q:
            def filter_by(self, **kw):
                self._name = kw.get("name")
                return self

            def first(self):
                if self._name in sess.KNOWN:
                    return types.SimpleNamespace(id=1, name=self._name)
                return None

        return _Q()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "oauth_redirect_base", "https://lios.example.ts.net")
    monkeypatch.setattr(
        auth_routes, "get_db", lambda: types.SimpleNamespace(session=lambda: _Sess()),
    )
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    return TestClient(app, follow_redirects=False)


def _login(client, *, account=ACCOUNT, user="alex", start=None):
    params = {"account": account, "user": user}
    if start is not None:
        params["start"] = start
    return client.get("/api/auth/google/login", params=params)


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def test_valid_start_redirects_to_google(client):
    resp = _login(client, start=oauth.sign_login_start(ACCOUNT, "alex"))
    assert resp.status_code in (302, 307), resp.text
    assert resp.headers["location"].startswith("https://accounts.google.com/o/oauth2/auth?")
    assert "login_hint=someone%40example.com" in resp.headers["location"]


def test_missing_start_is_403(client):
    resp = _login(client)
    assert resp.status_code == 403, resp.text
    assert "start" in resp.json()["error"]


def test_empty_start_is_403(client):
    assert _login(client, start="").status_code == 403


def test_start_minted_for_one_user_is_rejected_for_another(client):
    """The attack this exists to stop: take a legitimate link for yourself
    and edit `user=` to point the grant at somebody else's row."""
    start = oauth.sign_login_start(ACCOUNT, "alex")
    resp = _login(client, user="sam", start=start)
    assert resp.status_code == 403, resp.text


def test_start_minted_for_one_account_is_rejected_for_another(client):
    start = oauth.sign_login_start(ACCOUNT, "alex")
    resp = _login(client, account="other@example.com", start=start)
    assert resp.status_code == 403, resp.text


def test_expired_start_is_403(client):
    eleven_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=11)
    start = oauth.sign_login_start(ACCOUNT, "alex", now=eleven_minutes_ago)
    resp = _login(client, start=start)
    assert resp.status_code == 403, resp.text
    assert "expired" in resp.json()["error"]


def test_start_just_inside_ttl_is_accepted(client):
    nine_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=9)
    start = oauth.sign_login_start(ACCOUNT, "alex", now=nine_minutes_ago)
    assert _login(client, start=start).status_code in (302, 307)


def test_tampered_signature_is_403(client):
    start = oauth.sign_login_start(ACCOUNT, "alex")
    payload_b64, sig_b64 = start.split(".", 1)
    flipped = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
    assert _login(client, start=f"{payload_b64}.{flipped}").status_code == 403


def test_garbage_start_is_403_not_500(client):
    for junk in ("nodot", "a.b", "%%%.%%%", "eyJ4IjoxfQ.", "."):
        resp = _login(client, start=junk)
        assert resp.status_code == 403, (junk, resp.status_code, resp.text)


def test_a_state_token_cannot_be_replayed_as_a_start(client):
    """Same root secret, different HKDF label: a callback `state` (which a
    user legitimately sees in their own browser's URL bar) must not double as
    a `start` for the same account/user."""
    exp = int(datetime.now(timezone.utc).timestamp()) + 600
    as_state = oauth._sign_state({"account": ACCOUNT, "user": "alex", "exp": exp})
    assert _login(client, start=as_state).status_code == 403


def test_unknown_user_with_valid_start_is_still_400(client):
    """Ordering: the proof gates the route, then the existing user check runs."""
    start = oauth.sign_login_start(ACCOUNT, "nobody")
    resp = _login(client, user="nobody", start=start)
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Minting: GET /api/auth/google/login-url (session-gated, self unless admin)
# ---------------------------------------------------------------------------


def _as(client, *, name, is_admin):
    client.app.dependency_overrides[current_ui_user] = lambda: types.SimpleNamespace(
        id=1 if name == "alex" else 2, name=name, is_admin=is_admin,
    )
    return client


def test_login_url_for_self_is_minted_and_valid(client):
    resp = _as(client, name="sam", is_admin=False).get(
        "/api/auth/google/login-url", params={"account": ACCOUNT, "user": "sam"},
    )
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    assert url.startswith("/api/auth/google/login?")
    # The minted URL is accepted by the route it points at.
    follow = client.get(url)
    assert follow.status_code in (302, 307), follow.text


def test_login_url_for_someone_else_is_403_unless_admin(client):
    c = _as(client, name="sam", is_admin=False)
    resp = c.get("/api/auth/google/login-url", params={"account": ACCOUNT, "user": "alex"})
    assert resp.status_code == 403

    c = _as(client, name="alex", is_admin=True)
    resp = c.get("/api/auth/google/login-url", params={"account": ACCOUNT, "user": "sam"})
    assert resp.status_code == 200, resp.text


def test_login_url_route_is_session_gated():
    from app.main import is_session_gated

    assert is_session_gated("/api/auth/google/login-url")
    # …while the login leg itself stays exempt (the cross-domain reason).
    assert not is_session_gated("/api/auth/google/login")


# ---------------------------------------------------------------------------
# The helper every reauth link is built with
# ---------------------------------------------------------------------------


def test_google_login_url_encodes_and_verifies():
    url = oauth.google_login_url("a+b@example.com", "alex")
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    assert q["account"] == ["a+b@example.com"]
    assert q["user"] == ["alex"]
    oauth.verify_login_start(q["start"][0], account_email="a+b@example.com", user_name="alex")


def test_start_key_is_distinct_from_state_key():
    assert oauth._login_start_signing_key() != oauth._state_signing_key()


def test_missing_encryption_key_refuses_to_mint(monkeypatch):
    monkeypatch.setattr(settings, "oauth_encryption_key", "")
    with pytest.raises(RuntimeError, match="HOME_OAUTH_ENCRYPTION_KEY"):
        oauth.sign_login_start(ACCOUNT, "alex")
