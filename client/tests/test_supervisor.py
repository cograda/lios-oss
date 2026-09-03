"""TaskSupervisor — restart-with-backoff for daemon loops (ISS-001)."""

import asyncio

from lios_sync.supervisor import TaskSupervisor


def _run(coro):
    return asyncio.run(coro)


def test_crashed_task_is_restarted():
    async def scenario():
        shutdown = asyncio.Event()
        sup = TaskSupervisor(shutdown, backoff=(0,))
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError(f"boom {calls}")
            shutdown.set()

        task = sup.start("flaky", flaky)
        await asyncio.wait_for(task, timeout=5)
        return calls, sup.health()["flaky"]

    calls, health = _run(scenario())
    assert calls == 3  # crashed twice, succeeded third time
    assert health["restarts"] == 2
    assert "boom 2" in health["last_error"]


def test_clean_return_is_not_restarted():
    async def scenario():
        shutdown = asyncio.Event()
        sup = TaskSupervisor(shutdown, backoff=(0,))
        calls = 0

        async def disabled_loop():
            nonlocal calls
            calls += 1  # e.g. reminders loop with no EventKit store

        task = sup.start("disabled", disabled_loop)
        await asyncio.wait_for(task, timeout=5)
        return calls, sup.health()["disabled"]

    calls, health = _run(scenario())
    assert calls == 1
    assert health["finished"] is True
    assert health["restarts"] == 0


def test_cancellation_propagates():
    async def scenario():
        shutdown = asyncio.Event()
        sup = TaskSupervisor(shutdown, backoff=(0,))

        async def forever():
            await asyncio.sleep(3600)

        task = sup.start("forever", forever)
        await asyncio.sleep(0)  # let it start
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert _run(scenario()) is True


def test_shutdown_during_backoff_stops_restarting():
    async def scenario():
        shutdown = asyncio.Event()
        sup = TaskSupervisor(shutdown, backoff=(30,))
        calls = 0

        async def always_crashes():
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")

        task = sup.start("crasher", always_crashes)
        await asyncio.sleep(0.05)  # crash once, now waiting out backoff
        shutdown.set()
        await asyncio.wait_for(task, timeout=5)
        return calls

    assert _run(scenario()) == 1  # no restart after shutdown


def test_beat_tracks_liveness():
    async def scenario():
        shutdown = asyncio.Event()
        sup = TaskSupervisor(shutdown, backoff=(0,))

        async def loop():
            sup.beat("loop")
            shutdown.set()

        task = sup.start("loop", loop)
        await asyncio.wait_for(task, timeout=5)
        return sup.health()["loop"]

    health = _run(scenario())
    assert health["alive_seconds_ago"] is not None
    assert health["alive_seconds_ago"] < 5


def test_no_beats_task_reports_no_liveness_age():
    async def scenario():
        shutdown = asyncio.Event()
        sup = TaskSupervisor(shutdown, backoff=(0,))

        async def server():
            shutdown.set()

        task = sup.start("mcp", server, beats=False)
        await asyncio.wait_for(task, timeout=5)
        return sup.health()["mcp"]

    health = _run(scenario())
    assert health["alive_seconds_ago"] is None
