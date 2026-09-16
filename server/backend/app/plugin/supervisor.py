"""Startup-task supervision — V4 chunk 3.1.

Startup-kind background tasks (obsidian vault watcher, HA WebSocket
listener) are long-lived coroutines started once at boot and run until
shutdown. `supervise()` wraps one such coroutine so an unhandled exception
doesn't silently kill the task forever: it logs the failure, restarts with
exponential backoff (capped at 5 minutes), and exposes the task's current
state (`running` / `restarting` / `dead`) via an in-memory registry —
simple by design, no persistence, read via `GET /api/system/background-tasks`
(see `app/routes/system.py`).

`dead` is reserved for a task whose *supervisor* itself was cancelled or
told to stop while mid-crash-backoff; a task that keeps crashing just keeps
restarting (bounded backoff, not bounded retries) since these are meant to
run for the lifetime of the process.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 300.0  # 5 minutes


@dataclass
class TaskState:
    name: str
    status: str = "starting"  # "running" | "restarting" | "stopped" | "dead"
    restarts: int = 0
    last_error: str | None = None
    started_at: float | None = None
    updated_at: float = field(default_factory=time.time)

    def _touch(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.updated_at = time.time()


_registry: dict[str, TaskState] = {}


def get_task_states() -> dict[str, TaskState]:
    """Snapshot of every supervised task's current state (by name)."""
    return dict(_registry)


async def supervise(
    name: str,
    target: Callable[[asyncio.Event], Awaitable[None]],
    stop_event: asyncio.Event,
) -> None:
    """Run `target(stop_event)` under restart-with-backoff supervision.

    Returns once `stop_event` is set and the target has exited (cleanly or
    otherwise). Any exception from `target` is caught, logged, and followed
    by a backoff sleep (interruptible by `stop_event`) before retrying.
    """
    state = _registry.setdefault(name, TaskState(name=name))
    backoff = _INITIAL_BACKOFF_SECONDS

    while not stop_event.is_set():
        state._touch(status="running", started_at=time.time())
        try:
            await target(stop_event)
            # Target returned. If it did so because stop_event is set, this
            # is a clean shutdown. Otherwise the task ended on its own
            # (unexpected for a startup task) — treat like a crash so it
            # gets restarted rather than silently going dark.
            if stop_event.is_set():
                state._touch(status="stopped")
                return
            logger.warning("Startup task %r exited without stop_event; restarting", name)
            state._touch(status="restarting", last_error="task exited unexpectedly")
        except asyncio.CancelledError:
            state._touch(status="stopped")
            raise
        except Exception as e:
            logger.exception("Startup task %r crashed", name)
            state._touch(
                status="restarting",
                restarts=state.restarts + 1,
                last_error=str(e),
            )

        if stop_event.is_set():
            state._touch(status="stopped")
            return

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            state._touch(status="stopped")
            return
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

    state._touch(status="stopped")
