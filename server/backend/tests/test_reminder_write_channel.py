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

from app.integrations.apple_reminders import commands
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


class TestConcurrentDrainSafety:
    """Two daemons for the same user (a laptop + a work Mac — `stream_manager`
    supports exactly this) can subscribe within the same instant, and each
    subscribe runs its own `drain_pending`. Without exclusive claiming, both
    would re-dispatch the same pending row and the device would apply the
    same write twice."""

    def test_claim_is_exclusive(self, real_db_concurrent):
        """Two sessions racing to claim the same row: exactly one wins.

        Needs `real_db_concurrent` (independent connections), not `real_db`
        — the latter routes every session through one shared connection for
        speed, which would make this race meaningless (or unsafe)."""
        session_a = real_db_concurrent.SessionLocal()
        session_b = real_db_concurrent.SessionLocal()
        try:
            cmd = _cmd(session_a, summary="claim me")
            cmd_id = cmd.id

            first = commands._claim(session_a, cmd_id)
            second = commands._claim(session_b, cmd_id)

            assert first is True
            assert second is False

            row = session_b.get(ReminderCommand, cmd_id)
            session_b.refresh(row)
            assert row.status == "draining"
        finally:
            session_a.close()
            session_b.close()

    def test_two_concurrent_drains_never_both_dispatch_the_same_row(
        self, real_db_concurrent, monkeypatch
    ):
        """The real race: two daemons for the same user subscribe within the
        same instant, and both `drain_pending` calls run their read (see
        `_fetch_candidates`) before either has claimed anything — genuine
        concurrency, not a lucky interleaving. Both must not dispatch the
        same row; a `threading.Barrier` forces both reads to land before
        either claim, which is exactly the window `_claim` exists to close.

        Uses `real_db_concurrent` (independent connections), not `real_db` —
        two real threads must genuinely race against the same row, which a
        single shared connection can't safely do.
        """
        import threading

        setup_session = real_db_concurrent.SessionLocal()
        try:
            cmd = _cmd(setup_session, summary="raced by two daemons")
            cmd_id = cmd.id
        finally:
            setup_session.close()

        barrier = threading.Barrier(2)
        original_fetch = commands._fetch_candidates

        def _synced_fetch(session, *, user_id):
            rows = original_fetch(session, user_id=user_id)
            barrier.wait(timeout=5)
            return rows

        dispatched: list[int] = []
        lock = threading.Lock()

        def _fake_dispatch(session, *, user_id, user_name, action, args, timeout=3.0):
            with lock:
                dispatched.append(cmd_id)
            return {"applied": True}

        monkeypatch.setattr(commands, "_fetch_candidates", _synced_fetch)
        monkeypatch.setattr(commands, "dispatch_command", _fake_dispatch)

        results: list[dict] = []

        def _run():
            session = real_db_concurrent.SessionLocal()
            try:
                results.append(commands.drain_pending(session, user_id=1, user_name="alex"))
            finally:
                session.close()

        t1 = threading.Thread(target=_run)
        t2 = threading.Thread(target=_run)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert dispatched == [cmd_id], (
            f"the row was dispatched {len(dispatched)} time(s), expected exactly 1: {dispatched}"
        )
        total_claimed_elsewhere = sum(r["claimed_elsewhere"] for r in results)
        assert total_claimed_elsewhere == 1

    def test_acked_command_is_not_resent(self, db_session):
        """A command already marked `done` by `complete_command` must never
        be picked up by a later drain — it isn't `pending` any more."""
        cmd = _cmd(db_session, summary="finish me")
        assert commands.complete_command(
            db_session, command_id=cmd.id, user_id=1, result={"ok": True},
        )

        counts = commands.drain_pending(db_session, user_id=1, user_name="alex")

        db_session.refresh(cmd)
        assert cmd.status == "done"
        assert counts["considered"] == 0
        assert counts["replayed"] == 0


class TestStrandedClaimRecovery:
    """`_claim` moves a row `pending` -> `draining` before dispatching it.
    If the process that won the claim dies before finishing — a deploy
    restart, an OOM kill, one of the several-times-a-day container
    recreates — nothing was ever going to revisit that row on its own:
    `_fetch_candidates` only selects `pending`. A stranded `draining` row
    must be recovered, not silently invisible forever."""

    def test_stranded_draining_row_is_recovered_and_redispatched_once(
        self, db_session, monkeypatch,
    ):
        cmd = _cmd(db_session, summary="orphaned mid-claim")
        cmd.status = "draining"
        cmd.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db_session.commit()

        dispatched = []
        monkeypatch.setattr(
            commands, "dispatch_command",
            lambda *a, **k: dispatched.append(1) or {"applied": True},
        )

        counts = commands.drain_pending(db_session, user_id=1, user_name="alex")

        db_session.refresh(cmd)
        assert counts["recovered"] == 1
        assert dispatched == [1]
        assert cmd.status == "superseded"
        assert cmd.claimed_at is None

    def test_a_recent_draining_row_is_left_alone(self, db_session, monkeypatch):
        """A claim taken moments ago is presumably still in flight — recovery
        must not steal it out from under a dispatch that's still running."""
        cmd = _cmd(db_session, summary="actively being dispatched")
        cmd.status = "draining"
        cmd.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db_session.commit()

        dispatched = []
        monkeypatch.setattr(
            commands, "dispatch_command",
            lambda *a, **k: dispatched.append(1) or {"applied": True},
        )

        counts = commands.drain_pending(db_session, user_id=1, user_name="alex")

        db_session.refresh(cmd)
        assert counts["recovered"] == 0
        assert dispatched == []
        assert cmd.status == "draining"

    def test_pending_writes_counts_a_stranded_draining_row(self, db_session):
        """The axis-6 dead-subscription alert reads `facade.pending_writes()`
        — a write stuck in `draining` is exactly the write this whole channel
        exists to protect, so it must not vanish from that count."""
        from app.integrations.apple_reminders.facade import FACADE

        cmd = _cmd(db_session, summary="stranded")
        cmd.status = "draining"
        cmd.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db_session.commit()

        rows = FACADE.pending_writes(db_session)
        entry = next(r for r in rows if r["user_id"] == 1)
        assert entry["count"] == 1

    def test_expire_stale_also_expires_a_very_old_draining_row(self, db_session):
        """Backstop: if a claim were ever stranded past `MAX_REPLAY_AGE`
        itself (the 5-minute recovery having somehow not run), it must still
        retire to `expired` rather than sit in limbo forever."""
        cmd = _cmd(db_session, summary="ancient stranded claim")
        cmd.status = "draining"
        cmd.claimed_at = datetime.now(timezone.utc) - timedelta(hours=5)
        cmd.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
        db_session.commit()

        n = commands.expire_stale(db_session)

        db_session.refresh(cmd)
        assert n == 1
        assert cmd.status == "expired"
