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
bug — `backlog_sync` created rows it never dispatched, see that module). An
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


def expire_stale(session: Session, *, older_than: timedelta = MAX_REPLAY_AGE) -> int:
    """Mark commands too old to replay as `expired`, returning the count.

    Separated from `drain_pending` on purpose. If the reaper simply *skipped*
    old rows they would stay `pending` forever, and every future drain would
    re-scan them — plus the pending count would keep reading as "writes waiting
    to happen" when nothing will ever happen. `expired` is the honest terminal
    state: this write was lost, and here is the record of it.
    """
    cutoff = datetime.now(timezone.utc) - older_than
    rows = (
        session.query(ReminderCommand)
        .filter(ReminderCommand.status == "pending", ReminderCommand.created_at < cutoff)
        .all()
    )
    for cmd in rows:
        cmd.status = "expired"
        cmd.processed_at = datetime.now(timezone.utc)
    if rows:
        session.commit()
        logger.warning(
            "expired %d reminder command(s) older than %s — these writes were lost",
            len(rows), older_than,
        )
    return len(rows)


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
    expired = expire_stale(session)

    rows = (
        session.query(ReminderCommand)
        .filter(
            ReminderCommand.user_id == user_id,
            ReminderCommand.status == "pending",
        )
        .order_by(ReminderCommand.created_at)
        .limit(MAX_REPLAY_BATCH)
        .all()
    )

    counts = {"expired": expired, "replayed": 0, "failed": 0, "considered": len(rows)}
    for cmd in rows:
        try:
            payload = json.loads(cmd.payload) if cmd.payload else {}
        except (TypeError, ValueError):
            logger.warning("drain_pending: unreadable payload on cmd %s", cmd.id)
            counts["failed"] += 1
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
            counts["replayed"] += 1
        else:
            counts["failed"] += 1
            # Still no client. Stop rather than grinding through the batch —
            # the condition is per-connection, not per-command.
            break

    session.commit()
    if counts["replayed"] or counts["failed"]:
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
