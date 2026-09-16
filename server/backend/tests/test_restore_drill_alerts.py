"""R2 (2026-09-05): the two new `system_alerts` axes end to end against a
real (throwaway) Postgres — `restore_drill` (the weekly restore-drill
outcome) and `daemon_status` (per-daemon heartbeat/version/SSE attachment).

Exit check from the backlog item: "a deliberately broken dump raises the
alert" — here that's proven by seeding a failed `restore_drill` SyncState row
(exactly what `restore_drill.persist_result()` writes after a truncated dump
— see `tests/test_restore_drill.py` for that half) and asserting
`system_alerts` degrades. db tier — real `sync_state`/`client_tokens` rows.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


def _seed_restore_drill_state(session, *, status, days_ago, reason=None):
    from app.models.tokens import SyncState

    session.add(SyncState(
        integration="restore_drill",
        last_sync_status=status,
        last_sync_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        last_error=json.dumps({"ok": status == "ok", "reason": reason}),
    ))
    session.commit()


def _seed_daemon_token(session, *, label, user_id=1, last_seen_at=None, client_version="2.6.0"):
    from app.models.clients import ClientToken

    token = ClientToken.for_token(user_id=user_id, token=f"tok-{label}", label=label)
    token.client_version = client_version
    token.last_seen_at = last_seen_at
    session.add(token)
    session.commit()
    return token


def test_never_run_restore_drill_is_reported_not_silently_ok(db_session):
    from app.integrations.system.tools import handle_alerts_household

    payload = json.loads(handle_alerts_household(db_session, {}))

    assert payload["restore_drill"]["last_outcome"] == "never_run"
    assert payload["restore_drill"]["days_since_restore_drill"] is None
    entry = next((a for a in payload["alerts"] if a["integration"] == "restore_drill"), None)
    assert entry is not None
    assert any("never run" in issue for issue in entry["issues"])
    assert payload["status"] == "degraded"


def test_failed_restore_drill_degrades_system_alerts(db_session):
    """The literal exit check: a broken dump (recorded as a failed drill
    run) raises the alert."""
    from app.integrations.system.tools import handle_alerts_household

    _seed_restore_drill_state(db_session, status="error", days_ago=0, reason="dump missing PGDMP magic")

    payload = json.loads(handle_alerts_household(db_session, {}))

    assert payload["status"] == "degraded"
    assert payload["restore_drill"]["last_outcome"] == "error"
    entry = next(a for a in payload["alerts"] if a["integration"] == "restore_drill")
    assert any("FAILED" in issue for issue in entry["issues"])


def test_healthy_recent_restore_drill_raises_nothing(db_session):
    from app.integrations.system.tools import handle_alerts_household

    _seed_restore_drill_state(db_session, status="ok", days_ago=1)

    payload = json.loads(handle_alerts_household(db_session, {}))

    assert payload["restore_drill"]["last_outcome"] == "ok"
    assert payload["restore_drill"]["days_since_restore_drill"] == pytest.approx(1.0, abs=0.1)
    entry = next((a for a in payload["alerts"] if a["integration"] == "restore_drill"), None)
    assert entry is None


def test_stale_restore_drill_degrades(db_session):
    from app.integrations.system.tools import handle_alerts_household

    _seed_restore_drill_state(db_session, status="ok", days_ago=9)  # default threshold is 8

    payload = json.loads(handle_alerts_household(db_session, {}))

    entry = next(a for a in payload["alerts"] if a["integration"] == "restore_drill")
    assert any("stale" in issue for issue in entry["issues"])


def test_daemon_status_reports_heartbeat_version_and_sse(db_session):
    """Part B: last_heartbeat_age, daemon version, and SSE attachment are
    surfaced as data, not just folded into pass/fail issue text."""
    from app.integrations.system.tools import handle_alerts_household

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="alex-macbook", user_id=1,
        last_seen_at=now - timedelta(minutes=3), client_version="2.6.1",
    )

    payload = json.loads(handle_alerts_household(db_session, {}))

    row = next((d for d in payload["daemon_status"] if d["label"] == "alex-macbook"), None)
    assert row is not None
    assert row["client_version"] == "2.6.1"
    assert row["user_id"] == 1
    assert 0 <= row["last_heartbeat_age"] < 600
    # No one is subscribed to the SSE stream in this test, so the write
    # channel must read as NOT attached — the honest default.
    assert row["sse_connected"] is False


def test_daemon_status_present_even_when_healthy(db_session):
    """`daemon_status` is a data axis, always populated — unlike `alerts`,
    which only carries entries for something actually wrong."""
    from app.integrations.system.tools import handle_alerts_household

    now = datetime.now(timezone.utc)
    _seed_daemon_token(db_session, label="quiet-but-fine", last_seen_at=now - timedelta(minutes=1))

    payload = json.loads(handle_alerts_household(db_session, {}))

    assert any(d["label"] == "quiet-but-fine" for d in payload["daemon_status"])
    assert not any(a["integration"] == "quiet-but-fine" for a in payload["alerts"])
