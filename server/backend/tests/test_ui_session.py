"""Dashboard sessions on a real database (2026-09-06 — one credential).

`POST /api/auth/login` takes a per-user bearer and sets a cookie holding a
random session id; `resolve_session` re-checks the bearer and the user on
every request; `POST /api/auth/logout` deletes the row. db tier: these
assert on `ui_sessions` rows and on bearer/user state changes that only a
real `resolve_session` can see.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth import ui_session
from app.auth.hashing import hash_token
from app.auth.ui_session import SESSION_COOKIE
from app.models.clients import ClientToken
from app.models.ui_sessions import UiSession
from app.models.users import User

pytestmark = pytest.mark.db


def _mint(real_db, user_id: int, label: str = "test") -> tuple[int, str]:
    with real_db.session() as session:
        row, plaintext = ClientToken.mint(user_id=user_id, label=label)
        session.add(row)
        session.commit()
        return row.id, plaintext


@pytest.fixture
def client(real_db):
    from app.main import app

    # https: the session cookie is `secure`, and the jar will not send it over http.
    return TestClient(app, base_url="https://testserver")


def test_login_sets_a_session_cookie_that_is_not_the_bearer(real_db, client):
    token_id, bearer = _mint(real_db, 2)

    resp = client.post("/api/auth/login", json={"token": bearer})

    assert resp.status_code == 200, resp.text
    assert resp.json()["user"] == {
        "id": 2, "name": "sam", "display_name": "Sam", "is_admin": False,
    }
    cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie and cookie != bearer
    # The row holds a hash of the cookie and remembers the bearer that opened it.
    with real_db.session() as session:
        row = session.query(UiSession).one()
        assert row.session_hash == hash_token(cookie)
        assert row.user_id == 2
        assert row.client_token_id == token_id
        assert row.expires_at > datetime.now(timezone.utc) + timedelta(days=29)
    # Cookie attributes: httpOnly, secure, strict.
    set_cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=strict" in set_cookie
    # No legacy cookie is set any more.
    assert "ui_token=" not in set_cookie


def test_check_reports_the_signed_in_user_and_admin_flag(real_db, client):
    assert client.get("/api/auth/check").json() == {"authenticated": False, "user": None}

    _, bearer = _mint(real_db, 1)
    client.post("/api/auth/login", json={"token": bearer})

    check = client.get("/api/auth/check").json()
    assert check["authenticated"] is True
    assert check["user"]["name"] == "alex"
    assert check["user"]["is_admin"] is True  # migration/data fix: user 1 is admin


def test_wrong_bearer_is_401_and_opens_no_session(real_db, client):
    resp = client.post("/api/auth/login", json={"token": "not-a-real-token"})
    assert resp.status_code == 401
    assert SESSION_COOKIE not in client.cookies
    with real_db.session() as session:
        assert session.query(UiSession).count() == 0


def test_expired_bearer_cannot_sign_in(real_db, client):
    with real_db.session() as session:
        row, bearer = ClientToken.mint(user_id=2, label="old")
        row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        session.add(row)
        session.commit()
    assert client.post("/api/auth/login", json={"token": bearer}).status_code == 401


def test_session_gates_a_protected_route_and_binds_the_user(real_db, client):
    """End to end through the real app: `/api/preferences/{own id}` is
    reachable with a session, and the caller is bound as themselves."""
    assert client.get("/api/preferences/2").status_code == 401

    _, bearer = _mint(real_db, 2)
    client.post("/api/auth/login", json={"token": bearer})

    assert client.get("/api/preferences/2").status_code == 200
    # The middleware bound sam, so her own token list is what /clients shows.
    clients = client.get("/api/auth/clients").json()["clients"]
    assert {c["user_id"] for c in clients} == {2}


def test_revoking_the_bearer_kills_the_session_on_the_next_request(real_db, client):
    token_id, bearer = _mint(real_db, 2)
    client.post("/api/auth/login", json={"token": bearer})
    assert client.get("/api/preferences/2").status_code == 200

    with real_db.session() as session:
        session.query(ClientToken).filter_by(id=token_id).update({"is_active": False})
        session.commit()

    assert client.get("/api/preferences/2").status_code == 401
    assert client.get("/api/auth/check").json()["authenticated"] is False


def test_deactivating_the_user_kills_the_session(real_db, client):
    _, bearer = _mint(real_db, 2)
    client.post("/api/auth/login", json={"token": bearer})
    assert client.get("/api/preferences/2").status_code == 200

    with real_db.session() as session:
        session.query(User).filter_by(id=2).update({"is_active": False})
        session.commit()
    try:
        assert client.get("/api/preferences/2").status_code == 401
    finally:
        with real_db.session() as session:
            session.query(User).filter_by(id=2).update({"is_active": True})
            session.commit()


def test_expired_session_is_refused(real_db, client):
    _, bearer = _mint(real_db, 2)
    client.post("/api/auth/login", json={"token": bearer})
    with real_db.session() as session:
        session.query(UiSession).update(
            {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        session.commit()
    assert client.get("/api/preferences/2").status_code == 401


def test_logout_deletes_the_row_and_the_cookie_stops_working(real_db, client):
    _, bearer = _mint(real_db, 2)
    client.post("/api/auth/login", json={"token": bearer})
    cookie = client.cookies.get(SESSION_COOKIE)

    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    with real_db.session() as session:
        assert session.query(UiSession).count() == 0
    # Even if a copy of the cookie value survived, it resolves to nobody.
    assert ui_session.resolve_session(cookie) is None


def test_create_session_refuses_a_user_without_a_bearer():
    """A session must be tied to the bearer that opened it, or revoking the
    bearer could never revoke the session."""
    orphan = User(id=2, name="sam", display_name="Sam", is_active=True, is_admin=False)
    with pytest.raises(ValueError):
        ui_session.create_session(orphan)
