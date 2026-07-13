"""Server→client command dispatch for EventKit writes.

D.5 step 1: when a server-side MCP tool wants to add or complete a reminder,
it queues a row in `reminder_commands`, publishes an SSE event to the user's
connected daemon, and waits briefly for the daemon to ack via
POST /api/v1/reminders/commands/{id}/done.

If no client is connected (or doesn't ack in time), the call returns with
`queued=True` and the daemon will pick the row up on next reconnect via a
periodic reaper (TODO step 2.5).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from datetime import datetime, timezone

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
        return {"ok": True, "queued": True, "command_id": cmd_id, "reason": "loop unavailable"}

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
            "ok": True, "queued": True, "command_id": cmd_id,
            "reason": "no client connected",
        }

    try:
        result = future.result(timeout=timeout)
        return {"ok": True, "synced": True, "command_id": cmd_id, **(result or {})}
    except concurrent.futures.TimeoutError:
        _pending.pop(cmd_id, None)
        return {
            "ok": True, "queued": True, "command_id": cmd_id,
            "reason": "client did not ack in time",
        }
    except Exception as e:  # noqa: BLE001
        _pending.pop(cmd_id, None)
        return {"ok": False, "command_id": cmd_id, "error": str(e)}


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
