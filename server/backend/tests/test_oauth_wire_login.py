"""`/oauth/login` takes the per-user bearer (2026-09-06 — one credential).

The MCP OAuth login funnel: the SDK parks a claude.ai connection and sends
the human here. PR #123 deleted the Phase-0 form (shared UI token in, a
session as user_id=1 out). Now the form asks for the person's own bearer and
completes the login *as that user* — no default user, no other credential.

Unit tier: the real `build_oauth_routes()` output on a throwaway Starlette
app; `resolve_token_to_user` and `provider.complete_login` are replaced by
recorders so the wiring (which user the login completes for) is what is
asserted, without a database.
"""

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from app.auth import oauth_wire, rate_limit
from app.config import settings


def _user(uid: int):
    u = SimpleNamespace(id=uid, name=f"user{uid}", display_name=f"User {uid}", is_admin=False)
    u.client_token_id = 500 + uid
    return u


@pytest.fixture(autouse=True)
def _clean_limiter():
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(settings, "oauth_issuer", "https://lios.example.test")
    bearers = {"sams-bearer": _user(2), "alexs-bearer": _user(1)}
    monkeypatch.setattr(
        "app.auth.client_token.resolve_token_to_user", lambda token: bearers.get(token),
    )
    completed: list[tuple[str, int]] = []
    monkeypatch.setattr(
        oauth_wire.provider, "complete_login",
        lambda session_id, user_id: completed.append((session_id, user_id)) or "https://client/cb?code=x",
    )
    app = Starlette(routes=oauth_wire.build_oauth_routes())
    return TestClient(app), completed


def test_login_get_asks_for_the_per_user_bearer(harness):
    tc, _ = harness
    resp = tc.get("/oauth/login", params={"login_session": "abc"})
    assert resp.status_code == 200
    assert 'name="token"' in resp.text
    assert "config.toml" in resp.text  # where to find it
    assert 'name="ui_token"' not in resp.text
    assert 'value="abc"' in resp.text


def test_login_get_without_session_is_400(harness):
    tc, _ = harness
    assert tc.get("/oauth/login").status_code == 400


def test_valid_bearer_completes_the_login_as_that_user(harness):
    tc, completed = harness
    resp = tc.post(
        "/oauth/login", data={"login_session": "abc", "token": "sams-bearer"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://client/cb?code=x"
    assert completed == [("abc", 2)]


def test_a_different_bearer_completes_as_a_different_user(harness):
    """No default user: the identity comes from the bearer, nowhere else."""
    tc, completed = harness
    tc.post("/oauth/login", data={"login_session": "s1", "token": "alexs-bearer"}, follow_redirects=False)
    tc.post("/oauth/login", data={"login_session": "s2", "token": "sams-bearer"}, follow_redirects=False)
    assert completed == [("s1", 1), ("s2", 2)]


def test_invalid_or_missing_bearer_is_401_and_completes_nothing(harness):
    tc, completed = harness
    for body in ({"login_session": "abc"}, {"login_session": "abc", "token": "wrong"}):
        resp = tc.post("/oauth/login", data=body)
        assert resp.status_code == 401
        assert "Invalid token" in resp.text
    assert completed == []


def test_login_post_without_session_is_400(harness):
    tc, completed = harness
    assert tc.post("/oauth/login", data={"token": "sams-bearer"}).status_code == 400
    assert completed == []


def test_failed_guesses_spend_the_per_ip_budget(harness):
    tc, completed = harness
    last = None
    for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW + 1):
        last = tc.post("/oauth/login", data={"login_session": "abc", "token": "wrong"})
    assert last.status_code == 429
    assert completed == []


def test_expired_login_session_is_400(harness, monkeypatch):
    tc, _ = harness
    monkeypatch.setattr(oauth_wire.provider, "complete_login", lambda session_id, user_id: None)
    resp = tc.post("/oauth/login", data={"login_session": "gone", "token": "sams-bearer"})
    assert resp.status_code == 400


def test_phase0_scaffolding_stays_deleted():
    assert not hasattr(oauth_wire, "_PHASE0_USER_ID")
    assert not hasattr(settings, "oauth_phase0_enable")
    assert not hasattr(settings, "ui_token")
