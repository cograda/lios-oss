"""Supervision for the daemon's long-running asyncio loops.

ISS-001 root cause: loops started with a bare `asyncio.create_task()` die
silently when an unhandled exception escapes — the process keeps running
while e.g. the reminders push loop is dead, so `reminders_verified_at`
goes stale until someone notices and runs `launchctl kickstart -k`.

The supervisor owns each loop: it logs the crash loudly (the remote log
handler ships it to the server), restarts the loop with backoff, and
tracks per-task liveness so the heartbeat can report it.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger("comar.supervisor")

DEFAULT_BACKOFF = (5, 15, 60)


class TaskSupervisor:
    """Restart-with-backoff wrapper around the daemon's background tasks."""

    def __init__(
        self,
        shutdown_event: asyncio.Event,
        backoff: tuple[int, ...] = DEFAULT_BACKOFF,
    ):
        self._shutdown = shutdown_event
        self._backoff = backoff
        self._health: dict[str, dict] = {}

    def start(
        self,
        name: str,
        coro_factory: Callable[[], Awaitable[None]],
        beats: bool = True,
    ) -> asyncio.Task:
        """Run coro_factory under supervision, restarting on crash.

        beats=False for tasks that don't call beat() per iteration
        (e.g. a server that blocks in serve()) — their liveness age
        would otherwise read as a stall.
        """
        self._health[name] = {
            "restarts": 0,
            "last_error": None,
            "finished": False,
            "beats": beats,
        }
        self.beat(name)
        return asyncio.create_task(self._supervise(name, coro_factory), name=name)

    def beat(self, name: str) -> None:
        """Record that the named task is alive (call once per loop iteration)."""
        if name in self._health:
            self._health[name]["last_alive_at"] = time.monotonic()

    def health(self) -> dict:
        """Per-task health snapshot for the local /health endpoint and heartbeat."""
        now = time.monotonic()
        out = {}
        for name, h in self._health.items():
            out[name] = {
                "alive_seconds_ago": (
                    round(now - h["last_alive_at"], 1)
                    if h["beats"] and "last_alive_at" in h
                    else None
                ),
                "restarts": h["restarts"],
                "last_error": h["last_error"],
                "finished": h["finished"],
            }
        return out

    async def _supervise(
        self, name: str, coro_factory: Callable[[], Awaitable[None]]
    ) -> None:
        restarts = 0
        while not self._shutdown.is_set():
            self.beat(name)
            try:
                await coro_factory()
                # Returning without shutdown is a deliberate exit
                # (e.g. reminders loop when EventKit is unavailable).
                self._health[name]["finished"] = True
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — supervisor is the backstop
                restarts += 1
                self._health[name]["restarts"] = restarts
                self._health[name]["last_error"] = f"{type(e).__name__}: {e}"[:200]
                wait = self._backoff[min(restarts - 1, len(self._backoff) - 1)]
                logger.exception(
                    "Task %r crashed (restart #%d in %ds)", name, restarts, wait
                )
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
