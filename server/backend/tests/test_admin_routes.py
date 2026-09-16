"""Admin-only routes are enforced server-side (2026-09-06 — `users.is_admin`).

The classification lives in `app/auth/ui_session.py`'s module docstring; this
file drives the real app with two real sessions — alex (user 1, admin per the
migration's data fix) and sam (user 2, not admin) — and asserts the 403s
land where the docstring says they do, and nowhere else. db tier: sessions
and bearers are real rows.

The frontend hides the same controls for a non-admin, but that is a
courtesy; these tests are the gate.
"""

import pytest
from fastapi.testclient import TestClient

from app.models.clients import ClientToken
from app.models.tokens import OAuthToken

pytestmark = pytest.mark.db


def _signed_in(real_db, user_id: int) -> tuple[TestClient, int]:
    from app.main import app

    with real_db.session() as session:
        row, bearer = ClientToken.mint(user_id=user_id, label=f"admin-test-u{user_id}")
        session.add(row)
        session.commit()
        token_id = row.id
    client = TestClient(app, base_url="https://testserver")  # secure cookie
    resp = client.post("/api/auth/login", json={"token": bearer})
    assert resp.status_code == 200, resp.text
    return client, token_id


@pytest.fixture
def admin(real_db):
    return _signed_in(real_db, 1)[0]


@pytest.fixture
def member(real_db):
    return _signed_in(real_db, 2)


# --- credentials ----------------------------------------------------------

def test_only_an_admin_can_mint_a_bearer(admin, member):
    sam, _ = member
    body = {"user": "sam", "label": "phone"}
    assert sam.post("/api/auth/clients", json=body).status_code == 403
    assert admin.post("/api/auth/clients", json=body).status_code == 201


def test_member_lists_and_revokes_only_their_own_tokens(real_db, admin, member):
    sam, sam_token_id = member
    # The admin's own session token is user 1's — sam must not see it.
    listed = sam.get("/api/auth/clients").json()["clients"]
    assert listed and {c["user_id"] for c in listed} == {2}
    assert {c["user_id"] for c in admin.get("/api/auth/clients").json()["clients"]} == {1, 2}

    with real_db.session() as session:
        alex_row, _ = ClientToken.mint(user_id=1, label="alex-laptop")
        session.add(alex_row)
        session.commit()
        alex_token_id = alex_row.id

    assert sam.delete(f"/api/auth/clients/{alex_token_id}").status_code == 403
    assert sam.delete(f"/api/auth/clients/{sam_token_id}").status_code == 200
    assert admin.delete(f"/api/auth/clients/{alex_token_id}").status_code == 200


def test_oauth_token_list_is_filtered_to_the_caller_for_a_member(real_db, admin, member):
    sam, _ = member
    with real_db.session() as session:
        session.add(OAuthToken(
            user_id=1, provider="google", account_email="alex@example.com",
            access_token="a", refresh_token="r", scopes="s",
        ))
        session.add(OAuthToken(
            user_id=2, provider="google", account_email="sam@example.com",
            access_token="a", refresh_token="r", scopes="s",
        ))
        session.commit()
    assert {t["user"] for t in sam.get("/api/auth/tokens").json()["tokens"]} == {"sam"}
    assert {t["user"] for t in admin.get("/api/auth/tokens").json()["tokens"]} == {"alex", "sam"}


# --- server-wide state ----------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("delete", "/api/data/purge/weather?household=true", None),
    ("post", "/api/data/reindex-embeddings", None),
    ("put", "/api/integrations/weather/enabled", {"enabled": True}),
    ("post", "/api/integrations/weather/sync", None),
    ("get", "/api/integrations/weather/config", None),
    ("put", "/api/integrations/weather/config", {}),
    ("post", "/api/integrations/google_mail/backfill", None),
    ("post", "/api/integrations/lastfm/backfill", None),
    ("post", "/api/integrations/google_mail/embed", None),
    ("post", "/api/integrations/historical_corpus/ingest", None),
])
def test_server_wide_routes_are_403_for_a_member(member, method, path, body):
    sam, _ = member
    resp = getattr(sam, method)(path, json=body) if body is not None else getattr(sam, method)(path)
    assert resp.status_code == 403, f"{method.upper()} {path} → {resp.status_code}"


def test_server_wide_routes_are_not_403_for_the_admin(admin):
    """Not asserting success — several of these need live credentials — only
    that the admin gate lets the admin through to the handler."""
    assert admin.get("/api/integrations/weather/config").status_code == 200
    assert admin.put("/api/integrations/weather/enabled", json={"enabled": True}).status_code != 403
    assert admin.delete("/api/data/purge/weather?household=true").status_code != 403


# --- another person's data ------------------------------------------------

def test_member_may_touch_only_their_own_preferences(admin, member):
    sam, _ = member
    assert sam.get("/api/preferences/2").status_code == 200
    assert sam.get("/api/preferences/1").status_code == 403
    assert sam.put("/api/preferences/1", json={"daily_note.focus_count": 3}).status_code == 403
    assert admin.get("/api/preferences/2").status_code == 200
    assert admin.put("/api/preferences/2", json={"daily_note.focus_count": 3}).status_code == 200


def test_member_sees_only_their_own_logs(real_db, admin, member):
    from datetime import datetime, timezone

    from app.models.clients import ClientLog

    sam, _ = member
    now = datetime.now(timezone.utc)
    with real_db.session() as session:
        session.add(ClientLog(user_id=1, level="INFO", logger_name="t", message="alex-line", logged_at=now))
        session.add(ClientLog(user_id=2, level="INFO", logger_name="t", message="sam-line", logged_at=now))
        session.commit()

    # `?user=alex` cannot widen a member's view.
    mine = sam.get("/api/logs/?user=alex").json()["logs"]
    assert {r["user"] for r in mine} == {"sam"}
    assert {r["user"] for r in sam.get("/api/logs/tail?after_id=0").json()["logs"]} == {"sam"}
    assert sam.get("/api/logs/users").json()["users"] == ["sam"]

    everyone = admin.get("/api/logs/").json()["logs"]
    assert {r["user"] for r in everyone} == {"alex", "sam"}


def test_strava_connect_is_self_only_for_a_member(member, monkeypatch):
    sam, _ = member
    from app.integrations.strava import routes as strava_routes

    monkeypatch.setattr(strava_routes, "_credentials", lambda: ("cid", "secret"))
    assert sam.get("/api/strava/connect?user=alex", follow_redirects=False).status_code == 403
    # Their own row is fine (a 307 redirect to Strava).
    assert sam.get("/api/strava/connect?user=sam", follow_redirects=False).status_code in (302, 307)


# --- the household view stays readable ------------------------------------

def test_household_read_routes_stay_open_to_a_member(member):
    sam, _ = member
    for path in ("/api/integrations/", "/api/system/info", "/api/system/alerts", "/api/data/stats"):
        assert sam.get(path).status_code == 200, path
