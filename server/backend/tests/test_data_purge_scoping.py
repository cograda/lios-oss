"""F-security: `DELETE /api/data/purge/{integration}` used to be a bare
`DELETE FROM {table}` with no `user_id` at all — a household-wide delete
sitting behind the admin UI token, with no way to purge just one person's
data and no confirmation step for genuinely shared tables.

The signed-in admin is bound for the request since 2026-09-06, but a purge
must never quietly default to the *admin's* rows, so scoping still comes
from an explicit `?user=<name>` for per-user tables and an explicit
`?household=true` for shared ones. Real Postgres (`db` marker) because the
route issues raw `text(f"DELETE FROM {table}")` SQL against real tables —
faking that at the unit tier would just re-describe the SQL rather than
prove it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.db


def _app() -> FastAPI:
    from types import SimpleNamespace

    from app.auth.ui_session import require_admin
    from app.routes.data import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    # Purge is admin-only since 2026-09-06 (`require_admin`; see
    # `tests/test_admin_routes.py` for the 403). This suite is about the
    # per-user / household SCOPING of the delete itself, so the gate is
    # satisfied with a stand-in admin rather than a real session.
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        id=1, name="alex", is_admin=True,
    )
    return app


@pytest.fixture
def client(real_db):
    return TestClient(_app())


def _insert_scrobble(session, user_id: int, track: str, played_at: datetime) -> None:
    session.execute(
        text(
            "INSERT INTO scrobbles (user_id, track_name, artist_name, played_at, loved) "
            "VALUES (:uid, :track, 'Test Artist', :played_at, false)"
        ),
        {"uid": user_id, "track": track, "played_at": played_at},
    )
    session.commit()


def _scrobble_count(session, user_id: int | None = None) -> int:
    if user_id is None:
        return session.execute(text("SELECT count(*) FROM scrobbles")).scalar()
    return session.execute(
        text("SELECT count(*) FROM scrobbles WHERE user_id = :uid"), {"uid": user_id}
    ).scalar()


class TestPerUserTablePurge:
    """`scrobbles` (lastfm) has a `user_id` column — purge must be scoped to
    the user named by `?user=`, never a blanket DELETE."""

    def test_missing_user_param_is_refused_and_deletes_nothing(self, client, db_session):
        now = datetime.now(timezone.utc)
        _insert_scrobble(db_session, 1, "Song A", now)
        _insert_scrobble(db_session, 2, "Song B", now)

        response = client.delete("/api/data/purge/lastfm")

        assert response.status_code == 400
        assert "user" in response.json()["error"].lower()
        assert _scrobble_count(db_session) == 2

    def test_purge_scoped_to_named_user_leaves_other_user_intact(self, client, db_session):
        now = datetime.now(timezone.utc)
        _insert_scrobble(db_session, 1, "Alex's Song", now)
        _insert_scrobble(db_session, 2, "Sam's Song", now)

        response = client.delete("/api/data/purge/lastfm", params={"user": "alex"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["deleted"] == 1
        assert payload["user"] == "alex"
        assert _scrobble_count(db_session, user_id=1) == 0
        assert _scrobble_count(db_session, user_id=2) == 1, (
            "purging alex's data must never touch sam's rows"
        )

    def test_unknown_user_name_is_refused(self, client, db_session):
        _insert_scrobble(db_session, 1, "Song A", datetime.now(timezone.utc))
        response = client.delete("/api/data/purge/lastfm", params={"user": "nonexistent"})
        assert response.status_code == 400
        assert _scrobble_count(db_session) == 1

    def test_never_hardcodes_user_1_when_a_different_user_is_named(self, client, db_session):
        """Regression guard for the exact footgun this fix closes: naming
        sam must purge sam's rows, not silently fall back to user 1."""
        now = datetime.now(timezone.utc)
        _insert_scrobble(db_session, 1, "Alex's Song", now)
        _insert_scrobble(db_session, 2, "Sam's Song", now)

        response = client.delete("/api/data/purge/lastfm", params={"user": "sam"})

        assert response.status_code == 200
        assert response.json()["deleted"] == 1
        assert _scrobble_count(db_session, user_id=1) == 1
        assert _scrobble_count(db_session, user_id=2) == 0


class TestSharedTablePurge:
    """`transactions` (finance) has no `user_id` column at all — genuinely
    household-shared — so it requires the explicit `?household=true` flag."""

    def test_missing_household_flag_is_refused_and_deletes_nothing(self, client, db_session):
        db_session.execute(text(
            "INSERT INTO accounts (id, name, type) VALUES (1, 'Test Account', 'checking') "
            "ON CONFLICT (id) DO NOTHING"
        ))
        db_session.execute(text(
            "INSERT INTO categories (id, name, is_active) VALUES (1, 'Test Category', true) "
            "ON CONFLICT (id) DO NOTHING"
        ))
        db_session.commit()
        db_session.execute(text(
            "INSERT INTO transactions (account_id, date, description, amount, currency, category_id, is_manual_category, is_internal_transfer) "
            "VALUES (1, :d, 'test txn', -10.00, 'EUR', 1, false, false)"
        ), {"d": datetime.now(timezone.utc).date()})
        db_session.commit()

        response = client.delete("/api/data/purge/finance")

        assert response.status_code == 400
        assert "household" in response.json()["error"].lower()
        count = db_session.execute(text("SELECT count(*) FROM transactions")).scalar()
        assert count == 1

    def test_household_flag_purges_shared_table(self, client, db_session):
        db_session.execute(text(
            "INSERT INTO accounts (id, name, type) VALUES (1, 'Test Account', 'checking') "
            "ON CONFLICT (id) DO NOTHING"
        ))
        db_session.execute(text(
            "INSERT INTO categories (id, name, is_active) VALUES (1, 'Test Category', true) "
            "ON CONFLICT (id) DO NOTHING"
        ))
        db_session.commit()
        db_session.execute(text(
            "INSERT INTO transactions (account_id, date, description, amount, currency, category_id, is_manual_category, is_internal_transfer) "
            "VALUES (1, :d, 'test txn', -10.00, 'EUR', 1, false, false)"
        ), {"d": datetime.now(timezone.utc).date()})
        db_session.commit()

        response = client.delete("/api/data/purge/finance", params={"household": "true"})

        assert response.status_code == 200
        assert response.json()["deleted"] == 1
        count = db_session.execute(text("SELECT count(*) FROM transactions")).scalar()
        assert count == 0


class TestMixedIntegrationPurge:
    """`whatsapp` has one per-user table (whatsapp_messages) and one shared
    table (whatsapp_contacts) — the two flags are independent."""

    def test_user_only_purges_owned_table_and_skips_shared_one(self, client, db_session):
        db_session.execute(text(
            "INSERT INTO whatsapp_messages "
            "(user_id, message_id, chat_id, sender_id, timestamp, body, "
            " is_group, message_type, is_from_me) "
            "VALUES (1, 'msg-1', 'chat-1', 'alex', :ts, 'hi', false, 'text', false)"
        ), {"ts": datetime.now(timezone.utc)})
        db_session.execute(text(
            "INSERT INTO whatsapp_contacts (user_id, jid, name, is_group) "
            "VALUES (1, 'chat-1', 'Contact', false)"
        ))
        db_session.commit()

        response = client.delete(
            "/api/data/purge/whatsapp", params={"user": "alex"}
        )

        assert response.status_code == 200
        payload = response.json()
        assert "whatsapp_messages" in payload["purged_tables"]
        assert "whatsapp_contacts" in payload["skipped_tables"]

        remaining_messages = db_session.execute(
            text("SELECT count(*) FROM whatsapp_messages")
        ).scalar()
        remaining_contacts = db_session.execute(
            text("SELECT count(*) FROM whatsapp_contacts")
        ).scalar()
        assert remaining_messages == 0
        assert remaining_contacts == 1, "shared table must survive without ?household=true"


class TestUnknownIntegration:
    def test_unknown_integration_name_is_rejected(self, client):
        response = client.delete("/api/data/purge/not-a-real-integration")
        assert "error" in response.json()
