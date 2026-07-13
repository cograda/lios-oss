"""Tests for apple_reminders MCP tools (reminders_sync) — db tier.

Previously this file loaded tools.py via spec_from_file_location with a
hand-rolled sys.modules stub of the Reminder model (fake column sentinels,
MagicMock query chains mirroring the handler's exact call order). That
broke whenever the handler reordered a filter. The real-Postgres harness
makes the stubbing unnecessary: seed real rows, call the real handler.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.context import use_user
from app.integrations.apple_reminders.models import Reminder
from app.integrations.apple_reminders.tools import handle_sync_reminders

pytestmark = pytest.mark.db

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=7)
SINCE = NOW - timedelta(hours=12)


def _reminder(user_id=1, **kwargs):
    defaults = dict(
        uid=f"uid-{kwargs.get('summary', 'x')}",
        list_name="Reminders",
        summary="Test",
        priority=0,
        completed=False,
        created_at=OLD,
        synced_at=OLD,
    )
    defaults.update(kwargs)
    return Reminder(user_id=user_id, **defaults)


@pytest.fixture
def seeded(db_session):
    db_session.add_all([
        # Pre-existing open item, untouched since `since`.
        _reminder(summary="Open item", uid="open-1"),
        # Completed on the phone after `since`.
        _reminder(summary="Ticked off", uid="done-1", completed=True,
                  completed_date=NOW - timedelta(hours=1),
                  synced_at=NOW - timedelta(hours=1)),
        # Completed long before `since` — must NOT appear in completed_since.
        _reminder(summary="Old completion", uid="done-old", completed=True,
                  completed_date=OLD),
        # Added after `since`.
        _reminder(summary="Brand new", uid="new-1",
                  created_at=NOW - timedelta(hours=2),
                  synced_at=NOW - timedelta(hours=2)),
        # Pre-existing open item edited after `since` (synced_at bumped).
        _reminder(summary="Edited item", uid="edit-1",
                  synced_at=NOW - timedelta(hours=3)),
        # Sam's reminders — must never surface for user 1.
        _reminder(user_id=2, summary="Sam open", uid="n-open"),
        _reminder(user_id=2, summary="Sam done", uid="n-done", completed=True,
                  completed_date=NOW - timedelta(hours=1)),
    ])
    db_session.commit()
    return db_session


def _sync(session, args=None, user_id=1):
    with use_user(user_id):
        return json.loads(handle_sync_reminders(session, args or {}))


def test_sync_invalid_since_returns_error(db_session):
    result = _sync(db_session, {"since": "not-a-date"})
    assert "error" in result
    assert "not-a-date" in result["error"]


def test_sync_default_since_is_24h_ago(db_session):
    result = _sync(db_session)

    since_dt = datetime.fromisoformat(result["since"])
    delta = datetime.now(timezone.utc) - since_dt
    assert timedelta(hours=23, minutes=59) < delta < timedelta(hours=24, minutes=1)
    assert result["open"] == []
    assert result["completed_since"] == []
    assert result["added_since"] == []
    assert result["edited_since"] == []


def test_sync_returns_all_four_buckets(seeded):
    result = _sync(seeded, {"since": SINCE.isoformat()})

    assert result["since"] == SINCE.isoformat()
    # New + edited items are open too; the bucket is "all currently open".
    assert {r["summary"] for r in result["open"]} == {
        "Open item", "Brand new", "Edited item",
    }
    assert [r["summary"] for r in result["completed_since"]] == ["Ticked off"]
    assert result["completed_since"][0]["completed"] is True
    assert [r["summary"] for r in result["added_since"]] == ["Brand new"]
    assert [r["summary"] for r in result["edited_since"]] == ["Edited item"]


def test_sync_is_user_scoped(seeded):
    as_alex = json.dumps(_sync(seeded, {"since": SINCE.isoformat()}))
    assert "Sam" not in as_alex

    as_sam = _sync(seeded, {"since": SINCE.isoformat()}, user_id=2)
    assert {r["summary"] for r in as_sam["open"]} == {"Sam open"}
    assert [r["summary"] for r in as_sam["completed_since"]] == ["Sam done"]


def test_sync_synced_at_reflects_latest_change(seeded):
    result = _sync(seeded, {"since": SINCE.isoformat()})
    # MAX(synced_at) across user 1's rows is done-1 / new-1 territory, not OLD.
    latest = datetime.fromisoformat(result["synced_at"])
    assert latest > SINCE


def test_sync_naive_since_is_coerced_to_utc(db_session):
    result = _sync(db_session, {"since": "2026-04-06T00:00:00"})
    assert "error" not in result
    assert result["since"].endswith("+00:00")
