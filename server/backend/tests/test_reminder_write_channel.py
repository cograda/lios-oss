"""Tests for the reminders server→daemon write path.

The bug these pin was diagnosed in August, "fixed" with `launchctl kickstart -k`,
and recurred on 19 August — which is the signature of a workaround recorded as a
resolution. Three separate faults compounded:

  1. `dispatch_command` returned `ok: True, queued: True` when no client was
     connected, which is indistinguishable from success at the call site.
  2. Nothing ever drained the queue (`TODO step 2.5`, unbuilt for five months).
  3. `backlog_sync` wrote command rows it never dispatched, and its dedupe guard
     compared raw text against `json.dumps` output — so it re-queued every task
     with a quote or a non-ASCII character on every 30-minute run. 107,058 rows.

So the tests below are mostly about *not lying* and *not replaying history*.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.integrations.apple_reminders import backlog_sync, commands
from app.integrations.apple_reminders.models import ReminderCommand

pytestmark = pytest.mark.db


def _cmd(session, *, user_id=1, action="add", summary="x", status="pending", age_hours=0):
    row = ReminderCommand(
        user_id=user_id,
        action=action,
        payload=json.dumps({"args": {"summary": summary}}),
        status=status,
    )
    session.add(row)
    session.flush()
    if age_hours:
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    session.commit()
    return row


@pytest.fixture
def no_subscriber(monkeypatch):
    """Simulate the ACTUAL outage: an event loop exists, publish reaches nobody.

    ⚠️ Patching `stream_manager.loop = None` is *not* this scenario — it takes an
    earlier `loop unavailable` branch. Three tests here originally did exactly
    that while being named for this one, and a mutation reverting the honesty fix
    on the real branch passed all of them. Both branches now have coverage.
    """
    monkeypatch.setattr(commands.stream_manager, "loop", object())

    class _Fut:
        def result(self, timeout=None):
            return 0  # zero subscribers delivered to

    monkeypatch.setattr(
        commands.asyncio, "run_coroutine_threadsafe", lambda coro, loop: _Fut()
    )
    # The coroutine is never awaited in this path; close it so the test doesn't
    # emit "coroutine was never awaited".
    monkeypatch.setattr(
        commands.stream_manager, "publish", lambda *a, **k: None
    )


class TestQueuedIsNotSuccess:
    """`ok: True` on a write that did not happen is the root cause of the whole
    incident — the caller had no way to know, so nobody re-issued."""

    def test_no_client_connected_reports_failure(self, db_session, no_subscriber):
        result = commands.dispatch_command(
            db_session, user_id=1, user_name="alex", action="complete", args={"uid": "X"},
        )
        assert result["ok"] is False
        assert result["applied"] is False
        assert result["queued"] is True

    def test_no_client_reason_says_the_write_did_not_apply(self, db_session, no_subscriber):
        """A caller reading only `reason` must still learn the truth — the daily
        note and the MCP tool result both render this string."""
        result = commands.dispatch_command(
            db_session, user_id=1, user_name="alex", action="complete", args={"uid": "X"},
        )
        assert "not applied" in result["reason"].lower()

    def test_no_client_still_records_a_row_for_retry(self, db_session, no_subscriber):
        result = commands.dispatch_command(
            db_session, user_id=1, user_name="alex", action="complete", args={"uid": "X"},
        )
        row = db_session.get(ReminderCommand, result["command_id"])
        assert row.status == "pending"

    def test_missing_event_loop_also_reports_failure(self, db_session, monkeypatch):
        """The other early-return branch — a distinct failure, same lie."""
        monkeypatch.setattr(commands.stream_manager, "loop", None)
        result = commands.dispatch_command(
            db_session, user_id=1, user_name="alex", action="complete", args={"uid": "X"},
        )
        assert result["ok"] is False
        assert result["applied"] is False
        assert "not applied" in result["reason"].lower()


class TestExpiryCap:
    """The reaper must never replay history. When it was written the table held
    107,058 pending rows going back five months; draining them would have pushed
    every one into EventKit."""

    def test_old_rows_are_expired_not_replayed(self, db_session):
        old = _cmd(db_session, age_hours=5)
        n = commands.expire_stale(db_session)
        db_session.refresh(old)
        assert n == 1
        assert old.status == "expired"

    def test_recent_rows_survive_expiry(self, db_session):
        fresh = _cmd(db_session, age_hours=0)
        commands.expire_stale(db_session)
        db_session.refresh(fresh)
        assert fresh.status == "pending"

    def test_expired_is_terminal_not_skipped(self, db_session):
        """Skipping would leave the row `pending` forever, so every future drain
        re-scans it and the pending count keeps reading as "writes waiting"."""
        _cmd(db_session, age_hours=5)
        commands.expire_stale(db_session)
        assert commands.expire_stale(db_session) == 0

    def test_drain_expires_before_replaying(self, db_session, monkeypatch):
        old = _cmd(db_session, age_hours=99)
        monkeypatch.setattr(commands.stream_manager, "loop", None)
        counts = commands.drain_pending(db_session, user_id=1, user_name="alex")
        db_session.refresh(old)
        assert counts["expired"] == 1
        assert old.status == "expired"
        assert counts["replayed"] == 0

    def test_drain_is_batch_bounded(self, db_session, monkeypatch):
        """A pathological backlog must not spend the whole connection window
        pushing at a daemon that just woke up.

        ⚠️ Asserts an absolute ceiling, not `<= MAX_REPLAY_BATCH`. Comparing
        against the constant under test is true by construction — raising the
        constant to 100,000 left the original assertion passing.
        """
        for i in range(60):
            _cmd(db_session, summary=f"task {i}")
        monkeypatch.setattr(commands.stream_manager, "loop", None)
        counts = commands.drain_pending(db_session, user_id=1, user_name="alex")
        assert counts["considered"] <= 30, "drain must not scan an unbounded backlog"

    def test_drain_stops_on_first_undeliverable(self, db_session, monkeypatch):
        """No client is a per-connection condition, not per-command — grinding
        through 25 doomed dispatches wastes the subscribe path."""
        for i in range(5):
            _cmd(db_session, summary=f"task {i}")
        monkeypatch.setattr(commands.stream_manager, "loop", None)
        counts = commands.drain_pending(db_session, user_id=1, user_name="alex")
        assert counts["failed"] == 1

    def test_drain_is_scoped_to_one_user(self, db_session, monkeypatch):
        _cmd(db_session, user_id=1, summary="alex task")
        theirs = _cmd(db_session, user_id=2, summary="sam task")
        monkeypatch.setattr(commands.stream_manager, "loop", None)
        commands.drain_pending(db_session, user_id=1, user_name="alex")
        db_session.refresh(theirs)
        # Not expired (fresh) and not touched by the other user's drain.
        assert theirs.status == "pending"


class TestDedupeSurvivesJsonEscaping:
    """The 107k rows came from one line: `payload.contains(task.text[:50])`,
    comparing raw vault text to serialised JSON. Measured on live rows, the test
    returned False for every one — including a pure-ASCII task, because it
    contained quotes."""

    def test_a_quoted_task_matches_itself(self, db_session):
        text = '**Agree a "no more stuff coming into the house" rule**'
        _cmd(db_session, summary=text)
        assert backlog_sync._already_queued(db_session, user_id=1, summary=text) is True

    def test_an_em_dash_task_matches_itself(self, db_session):
        """`json.dumps` with default ensure_ascii turns `—` into `\\u2014`."""
        text = "**Truly clear the decks — not just move everything to one corner**"
        _cmd(db_session, summary=text)
        assert backlog_sync._already_queued(db_session, user_id=1, summary=text) is True

    def test_an_emoji_task_matches_itself(self, db_session):
        text = "Empty the washer 🔺 📅 2026-08-06"
        _cmd(db_session, summary=text)
        assert backlog_sync._already_queued(db_session, user_id=1, summary=text) is True

    def test_a_different_task_does_not_match(self, db_session):
        _cmd(db_session, summary="Ring the plumber")
        assert backlog_sync._already_queued(
            db_session, user_id=1, summary="Ring the electrician"
        ) is False

    def test_tasks_sharing_a_long_prefix_are_distinct(self, db_session):
        """The old guard compared only the first 50 characters, so two tasks
        with the same opening phrase collapsed and the second was dropped."""
        a = "Ring around for quotes on the materials list for the utility room"
        b = "Ring around for quotes on the materials list for the back bedroom"
        assert a[:50] == b[:50]
        _cmd(db_session, summary=a)
        assert backlog_sync._already_queued(db_session, user_id=1, summary=b) is False

    def test_dedupe_is_per_user(self, db_session):
        _cmd(db_session, user_id=2, summary="Sam's task")
        assert backlog_sync._already_queued(
            db_session, user_id=1, summary="Sam's task"
        ) is False

    def test_abandoned_rows_do_not_block_requeue(self, db_session):
        """The guard filters on `pending`. An abandoned row is a record of a lost
        write, not a reason to skip the retry."""
        _cmd(db_session, summary="Do the thing", status="abandoned")
        assert backlog_sync._already_queued(
            db_session, user_id=1, summary="Do the thing"
        ) is False


class TestVaultPushIsGatedOff:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(
            backlog_sync, "plugin_config",
            lambda name: SimpleNamespace(reminders_push_vault_to_reminders=False),
            raising=False,
        )
        assert backlog_sync._vault_push_enabled() is False

    def test_a_config_read_failure_does_not_enable_writes(self, monkeypatch):
        """Fail closed. Defaulting a write path on when config is unreadable is
        how a five-month no-op becomes a surprise flood of reminders."""
        import app.plugin.config_store as store

        def _boom(name):
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(store, "plugin_config", _boom)
        assert backlog_sync._vault_push_enabled() is False
