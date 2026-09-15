"""Startup-task supervision (unit tier) — V4 chunk 3.1.

`app.plugin.supervisor.supervise()` wraps a long-lived startup task so an
unhandled exception restarts it (with backoff) instead of silently killing
it, and `stop_event` cleanly ends supervision. These tests collapse the
backoff to ~0 so they run fast.
"""

from __future__ import annotations

import asyncio

import pytest

from app.plugin import supervisor

pytestmark = pytest.mark.unit


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(supervisor, "_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(supervisor, "_MAX_BACKOFF_SECONDS", 0.02)


@pytest.mark.anyio
async def test_crashing_task_is_restarted_with_backoff():
    calls = []

    async def flaky(stop_event: asyncio.Event) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(f"boom {len(calls)}")
        await stop_event.wait()

    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.supervise("flaky_task", flaky, stop_event))

    # Give it time to crash twice and succeed on the third attempt.
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    assert len(calls) == 3

    state = supervisor.get_task_states()["flaky_task"]
    assert state.restarts == 2
    assert state.status == "running"

    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    state = supervisor.get_task_states()["flaky_task"]
    assert state.status == "stopped"


@pytest.mark.anyio
async def test_stop_event_terminates_cleanly_without_crash():
    started = asyncio.Event()

    async def well_behaved(stop_event: asyncio.Event) -> None:
        started.set()
        await stop_event.wait()

    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.supervise("clean_task", well_behaved, stop_event))

    await asyncio.wait_for(started.wait(), timeout=2)
    assert supervisor.get_task_states()["clean_task"].status == "running"

    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    state = supervisor.get_task_states()["clean_task"]
    assert state.status == "stopped"
    assert state.restarts == 0
