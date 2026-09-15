"""Server→client command dispatch for EventKit writes.

D.5 step 1: when a server-side MCP tool wants to add or complete a reminder,
it queues a row in `reminder_commands`, publishes an SSE event to the user's
connected daemon, and waits briefly for the daemon to ack via
POST /api/v1/reminders/commands/{id}/done.

If no client is connected (or doesn't ack in time), the call returns with
`applied=False` and the row stays `pending` for `drain_pending()` to retry.

**`TODO step 2.5` — the reaper — went unbuilt for five months, and the cost was
exactly what the TODO implied.** Nothing drained the table, so every write made
while the daemon's SSE subscription was dead was lost silently: 57 of the 114
`complete` commands ever issued sat pending forever, and callers were told
`ok: true`. The first fix is therefore not the reaper but the *reporting* —
`ok: true, queued: true` is indistinguishable from success at the call site,
which is why the same failure was diagnosed in August, "fixed" with
`launchctl kickstart -k`, and recurred.

So a queued command now returns `ok: False` with `applied: False`. It is not a
success; the row is a retry record, not a receipt.

⚠️ **`drain_pending` is hard-capped by age (`MAX_REPLAY_AGE`).** When it was
written there were 107,058 pending `add` rows going back five months (a separate
bug — the now-deleted `backlog_sync` module created rows it never dispatched;
see server/CLAUDE.md's Known Issues entry for the history). An
uncapped reaper would have replayed all of them into EventKit. A reaper is a
retry mechanism, not a replay-history mechanism, and the cap is what keeps those
two apart.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.integrations.apple_reminders.models import ReminderCommand
from app.stream_manager import stream_manager

logger = logging.getLogger(__name__)

# command_id → Future resolved when the client posts /done. Threadsafe;
# tool handlers run in asyncio.to_thread() workers, the HTTP /done route
# runs on the main loop.
_pending: dict[int, concurrent.futures.Future] = {}


def dispatch_command(
    session: Session,
    *,
    user_id: int,
    user_name: str,
    action: str,
    args: dict,
    timeout: float = 3.0,
) -> dict:
    """Queue a command, publish via SSE, wait up to `timeout` for ack."""
    cmd = ReminderCommand(
        user_id=user_id,
        action=action,
        payload=json.dumps({"args": args}),
        status="pending",
    )
    session.add(cmd)
    session.commit()
    session.refresh(cmd)
    cmd_id = cmd.id

    loop = stream_manager.loop
    if loop is None:
        logger.warning("dispatch_command: no event loop bound, returning queued")
        return {
            "ok": False, "applied": False, "queued": True, "command_id": cmd_id,
            "reason": "server event loop unavailable — write not applied, will retry",
        }

    future: concurrent.futures.Future = concurrent.futures.Future()
    _pending[cmd_id] = future

    event = {
        "type": "eventkit_command",
        "command_id": cmd_id,
        "user_id": user_id,
        "action": action,
        "args": args,
    }

    publish_fut = asyncio.run_coroutine_threadsafe(
        stream_manager.publish(event, target_user=user_name), loop,
    )
    try:
        delivered = publish_fut.result(timeout=2.0)
    except Exception:
        logger.exception("dispatch_command: publish failed")
        delivered = 0

    if not delivered:
        _pending.pop(cmd_id, None)
        return {
            "ok": False, "applied": False, "queued": True, "command_id": cmd_id,
            "reason": (
                "no client connected — write NOT applied. Queued for retry when "
                "the daemon reconnects."
            ),
        }

    try:
        result = future.result(timeout=timeout)
        return {
            "ok": True, "applied": True, "synced": True, "command_id": cmd_id,
            **(result or {}),
        }
    except concurrent.futures.TimeoutError:
        _pending.pop(cmd_id, None)
        return {
            "ok": False, "applied": False, "queued": True, "command_id": cmd_id,
            "reason": (
                "client did not ack in time — write may not be applied. Queued "
                "for retry."
            ),
        }
    except Exception as e:  # noqa: BLE001
        _pending.pop(cmd_id, None)
        return {"ok": False, "applied": False, "command_id": cmd_id, "error": str(e)}


# ⚠️ The reaper will never replay a command older than this. Non-negotiable
# safety bound, not a tuning knob: on 2026-08-19 the table held 107,058 pending
# `add` rows spanning five months, and an uncapped drain would have pushed every
# one into EventKit. A write nobody has thought about for two hours should not
# silently materialise as a reminder — the caller has long since moved on, and if
# it mattered they re-issued it (which is exactly what happened tonight).
MAX_REPLAY_AGE = timedelta(hours=2)

# Per-drain ceiling, so a pathological backlog can't spend the whole SSE
# connection window pushing commands at a daemon that just woke up.
MAX_REPLAY_BATCH = 25

# ⚠️ How long a `_claim` (status='draining') is trusted to still be in
# progress. `_claim` moves a row off `pending` *before* `dispatch_command`
# runs, and if the process dies in between — a deploy restart, an OOM kill,
# one of the several-times-a-day container recreates — nothing was ever
# going to revisit that row: `_fetch_candidates` only selects `pending`, so
# a stranded `draining` row is invisible to every future drain, and
# `expire_stale`'s original filter was `pending`-only too, so it would have
# sat there past `MAX_REPLAY_AGE` and never even reached the honest
# `expired` state — the exact write this whole channel exists to protect
# would go silently missing, permanently, which is worse than the bug this
# module was built to fix. Short relative to `MAX_REPLAY_AGE` (2h) because a
# real in-flight claim resolves in the time it takes `dispatch_command` to
# get an ack or time out (seconds), never minutes.
DRAINING_CLAIM_TIMEOUT = timedelta(minutes=5)


def expire_stale(
    session: Session,
    *,
    older_than: timedelta = MAX_REPLAY_AGE,
    user_id: int | None = None,
) -> int:
    """Mark commands too old to replay as `expired`, returning the count.

    `user_id` confines the reap to one user's rows. `drain_pending` passes
    the reconnecting daemon's user: unscoped, one daemon coming back online
    expired the OTHER user's queued writes, whose own daemon may simply have
    been offline for the weekend (2026-09-06 scoping audit). `None` keeps
    the household-wide reap for any caller with no user in hand.

    Separated from `drain_pending` on purpose. If the reaper simply *skipped*
    old rows they would stay `pending` forever, and every future drain would
    re-scan them — plus the pending count would keep reading as "writes waiting
    to happen" when nothing will ever happen. `expired` is the honest terminal
    state: this write was lost, and here is the record of it.

    Also expires a `draining` row past this same age — belt and braces
    alongside `_recover_stranded_claims`' much shorter `DRAINING_CLAIM_TIMEOUT`
    reset. If a claim were ever stranded for the *entire* replay window (the
    5-minute recovery having somehow not run), this is the backstop that
    still retires it as a lost write instead of leaving it in limbo forever.
    """
    cutoff = datetime.now(timezone.utc) - older_than
    q = session.query(ReminderCommand).filter(
        ReminderCommand.status.in_(("pending", "draining")),
        ReminderCommand.created_at < cutoff,
    )
    if user_id is not None:
        q = q.filter(ReminderCommand.user_id == user_id)
    rows = q.all()
    for cmd in rows:
        cmd.status = "expired"
        cmd.processed_at = datetime.now(timezone.utc)
        cmd.claimed_at = None
    if rows:
        session.commit()
        logger.warning(
            "expired %d reminder command(s) older than %s — these writes were lost",
            len(rows), older_than,
        )
    return len(rows)


def _recover_stranded_claims(
    session: Session, *, user_id: int, timeout: timedelta = DRAINING_CLAIM_TIMEOUT
) -> int:
    """Reset any `draining` row for this user whose claim is older than
    `timeout` back to `pending`, returning the count recovered.

    `_claim` wins exclusivity by moving a row off `pending` *before*
    `dispatch_command` actually runs. If the process that won the claim dies
    before finishing — a deploy restart, an OOM kill, a container recreate —
    the row is stranded in `draining` with nothing left to finish the job or
    put it back. Run at the top of every `drain_pending` (before
    `_fetch_candidates`, which only ever selects `pending`) so the very next
    subscribe — this one, or another daemon's — recovers it immediately
    rather than waiting for `expire_stale`'s much longer `MAX_REPLAY_AGE` to
    quietly write it off as a lost cause it never needed to be.
    """
    cutoff = datetime.now(timezone.utc) - timeout
    updated = (
        session.query(ReminderCommand)
        .filter(
            ReminderCommand.user_id == user_id,
            ReminderCommand.status == "draining",
            ReminderCommand.claimed_at < cutoff,
        )
        .update({"status": "pending", "claimed_at": None}, synchronize_session=False)
    )
    if updated:
        session.commit()
        logger.warning(
            "recovered %d stranded 'draining' reminder command(s) for user %d "
            "(claim older than %s) back to pending",
            updated, user_id, timeout,
        )
    return updated


def _claim(session: Session, cmd_id: int) -> bool:
    """Atomically claim one pending command for draining, returning whether
    *this* call won the claim.

    Two daemons for the same user (a laptop + a work Mac, per
    `stream_manager`'s own docstring) can subscribe within the same instant,
    and each subscribe runs its own `drain_pending`. Without a claim, both
    would select the same pending rows, both dispatch a *second* live command
    for each, and the device would apply the same write twice.

    This is a conditional `UPDATE ... WHERE status = 'pending'`, not a
    `SELECT ... FOR UPDATE`. A row lock would not survive the loop below:
    `dispatch_command` commits its own transaction (it writes the retry row),
    and that inner commit releases any lock this transaction was holding —
    reopening the exact window a lock exists to close. An atomic conditional
    UPDATE has no such window: Postgres serializes concurrent UPDATEs against
    the same row regardless of when either side's transaction commits, and
    the loser's UPDATE simply matches zero rows because the winner already
    moved the row off `pending`.
    """
    updated = (
        session.query(ReminderCommand)
        .filter(ReminderCommand.id == cmd_id, ReminderCommand.status == "pending")
        .update(
            {"status": "draining", "claimed_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
    )
    session.commit()
    return updated == 1


def _fetch_candidates(session: Session, *, user_id: int) -> list[ReminderCommand]:
    """The read half of `drain_pending`, split out so a test can put a
    synchronization point between "two concurrent drains both saw this row
    as pending" and "one of them claims it" — the exact window `_claim`
    exists to close. Not otherwise meant to be called on its own.
    """
    return (
        session.query(ReminderCommand)
        .filter(
            ReminderCommand.user_id == user_id,
            ReminderCommand.status == "pending",
        )
        .order_by(ReminderCommand.created_at)
        .limit(MAX_REPLAY_BATCH)
        .all()
    )


def drain_pending(session: Session, *, user_id: int, user_name: str) -> dict:
    """Re-dispatch this user's recent pending commands. The missing step 2.5.

    Called when a daemon (re)subscribes to the SSE stream, which is the moment
    the thing that made them undeliverable stops being true. Oldest first, so
    commands land in the order they were issued — a `complete` replayed before
    the `add` it refers to would fail.

    Returns counts rather than raising: this runs on the subscribe path, and a
    failure to drain must never stop the client connecting. A daemon that is
    connected but hasn't drained still works for every future write; one that
    can't connect works for none.
    """
    recovered = _recover_stranded_claims(session, user_id=user_id)
    expired = expire_stale(session, user_id=user_id)

    rows = _fetch_candidates(session, user_id=user_id)

    counts = {
        "expired": expired, "replayed": 0, "failed": 0, "considered": len(rows),
        # Draining rows recovered back to `pending` this call because their
        # claim was older than `DRAINING_CLAIM_TIMEOUT` — the process that
        # claimed them died before finishing. Not counted in `considered`:
        # they were fetched by neither `_fetch_candidates` call this drain
        # made (the recovery runs first), only made eligible for the next one.
        "recovered": recovered,
        # Rows another concurrent drain claimed first — considered, but never
        # touched by this call. Distinct from `failed` (which means WE tried
        # to dispatch and it didn't land) so the two are never confused.
        "claimed_elsewhere": 0,
    }
    for cmd in rows:
        if not _claim(session, cmd.id):
            counts["claimed_elsewhere"] += 1
            continue

        try:
            payload = json.loads(cmd.payload) if cmd.payload else {}
        except (TypeError, ValueError):
            logger.warning("drain_pending: unreadable payload on cmd %s", cmd.id)
            counts["failed"] += 1
            # Release the claim — a broken payload isn't a delivery failure
            # tied to "no client connected", so it must not stop the batch,
            # but it also isn't resolved, so it stays pending for the next
            # attempt (and for a human to notice the same error recur).
            cmd.status = "pending"
            cmd.claimed_at = None
            session.commit()
            continue

        args = payload.get("args", payload)
        result = dispatch_command(
            session,
            user_id=user_id,
            user_name=user_name,
            action=cmd.action,
            args=args,
        )
        if result.get("applied"):
            # `dispatch_command` created a *new* row for the retry, so retire
            # the original rather than leaving two pending records of one write.
            cmd.status = "superseded"
            cmd.processed_at = datetime.now(timezone.utc)
            cmd.claimed_at = None
            counts["replayed"] += 1
            session.commit()
        else:
            # Still no client. Release the claim back to `pending` so a
            # later drain can retry, and stop rather than grinding through
            # the batch — the condition is per-connection, not per-command.
            cmd.status = "pending"
            cmd.claimed_at = None
            counts["failed"] += 1
            session.commit()
            break

    if counts["replayed"] or counts["failed"] or counts["claimed_elsewhere"] or counts["recovered"]:
        logger.info("drain_pending [%s]: %s", user_name, counts)
    return counts


def complete_command(
    session: Session,
    *,
    command_id: int,
    user_id: int,
    result: dict | None = None,
    error: str | None = None,
) -> bool:
    """Mark a queued command done (or failed) and resolve the waiting Future."""
    cmd = session.get(ReminderCommand, command_id)
    if cmd is None:
        return False
    if cmd.user_id != user_id:
        # Don't let a token complete another user's command.
        logger.warning(
            "complete_command: user %d tried to complete cmd %d owned by user %d",
            user_id, command_id, cmd.user_id,
        )
        return False

    cmd.status = "done" if error is None else "failed"
    cmd.processed_at = datetime.now(timezone.utc)
    payload = json.loads(cmd.payload) if cmd.payload else {}
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    cmd.payload = json.dumps(payload)
    session.commit()

    fut = _pending.pop(command_id, None)
    if fut and not fut.done():
        if error is not None:
            fut.set_exception(RuntimeError(error))
        else:
            fut.set_result(result or {})
    return True
