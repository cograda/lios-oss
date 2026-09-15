"""Unit tier — the embedding cron jobs' drain loop, in isolation.

`_drain_queue` (`app.integrations.embedding.tasks`) has no DB/session
concerns of its own: it just calls a bounded unit of work repeatedly until
it's exhausted, a time budget elapses, or it raises. Both
`run_embedding_processor` and `run_embedding_space_backfill` build on it, so
testing it here with fakes covers the loop shape for both without touching
Postgres.
"""

from __future__ import annotations

import asyncio

import pytest

from app.integrations.embedding import tasks
from app.integrations.embedding.manifest import MANIFEST

pytestmark = pytest.mark.unit


class _FakeClock:
    """Advances by a fixed step on every read — deterministic without
    real sleeps."""

    def __init__(self, step: float = 1.0):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def test_drain_queue_loops_until_a_batch_returns_zero():
    calls = []

    def process_batch():
        calls.append(1)
        # Two real batches, then empty.
        return 5 if len(calls) < 3 else 0

    total, batches, error = tasks._drain_queue(
        process_batch, budget_seconds=1000, clock=_FakeClock(step=0.01)
    )

    assert total == 10  # sum of the batches, not counting the trailing 0
    assert batches == 2
    assert error is None
    assert len(calls) == 3  # the zero-returning call happened and stopped the loop


def test_drain_queue_stops_when_the_budget_elapses_even_with_items_left():
    calls = []

    def process_batch():
        calls.append(1)
        return 5  # always more work available — budget must be what stops this

    clock = _FakeClock(step=10.0)  # each check burns 10s of the fake clock
    total, batches, error = tasks._drain_queue(
        process_batch, budget_seconds=25, clock=clock
    )

    # Budget is checked before each call; with a 10s-per-check clock and a
    # 25s budget, the loop runs while elapsed < 25 -- so it must stop well
    # short of an unbounded number of calls, and never claim "no error".
    assert error is None
    assert batches == len(calls)
    assert batches < 100  # would be unbounded if the budget were not enforced
    assert total == batches * 5


def test_drain_queue_budget_check_is_strictly_less_than_at_the_boundary():
    """Pins the exact boundary: elapsed == budget must stop the loop, not
    let one more batch through (a `<=` here would run one call too many)."""
    calls = []

    def process_batch():
        calls.append(1)
        return 1

    # start=0 (consumes 0); check 1 -> elapsed 1 (<2, proceeds, batch 1);
    # check 2 -> elapsed 2 (== budget, must stop here, not proceed).
    total, batches, error = tasks._drain_queue(
        process_batch, budget_seconds=2, clock=_FakeClock(step=1.0)
    )

    assert error is None
    assert batches == 1
    assert total == 1
    assert len(calls) == 1


def test_drain_queue_stops_on_exception_and_counts_earlier_batches():
    calls = []

    def process_batch():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return 5

    total, batches, error = tasks._drain_queue(
        process_batch, budget_seconds=1000, clock=_FakeClock(step=0.01)
    )

    assert batches == 1  # only the first (successful) batch counted
    assert total == 5
    assert error is not None
    assert "boom" in error
    assert len(calls) == 2  # the loop stopped after the failing call, no third


def test_drain_queue_makes_zero_calls_when_already_past_budget():
    calls = []

    def process_batch():
        calls.append(1)
        return 5

    total, batches, error = tasks._drain_queue(
        process_batch, budget_seconds=0, clock=_FakeClock(step=1.0)
    )

    assert calls == []
    assert (total, batches, error) == (0, 0, None)


def test_processor_budget_config_defaults_to_240():
    spec = MANIFEST.config_schema["processor_budget_seconds"]
    assert spec.default == 240
    assert spec.required is False


def test_space_backfill_budget_config_defaults_to_2400():
    spec = MANIFEST.config_schema["space_backfill_budget_seconds"]
    assert spec.default == 2400
    assert spec.required is False


def test_run_embedding_processor_records_error_on_sync_state_and_stops_quietly(monkeypatch):
    """A failing batch (surfaced by `_run_embedding_blocking` as an error
    string) must not raise out of the coroutine — it's logged and recorded
    on SyncState the same way other manifest cron jobs do, so the *next*
    tick retries rather than the scheduler seeing an unhandled exception."""
    monkeypatch.setattr(
        tasks, "_run_embedding_blocking", lambda: (5, 1, "boom: batch failed")
    )

    recorded = {}

    def _fake_update_sync_state(name, status, error=None, trigger=None, **kwargs):
        recorded.update(name=name, status=status, error=error, trigger=trigger)

    import app.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "_update_sync_state", _fake_update_sync_state)

    asyncio.run(tasks.run_embedding_processor())

    assert recorded["name"] == "embedding"
    assert recorded["status"] == "error"
    assert "boom" in recorded["error"]
    assert recorded["trigger"] == "embedding_processor"


def test_run_embedding_processor_does_not_touch_sync_state_on_success(monkeypatch):
    monkeypatch.setattr(tasks, "_run_embedding_blocking", lambda: (7, 2, None))

    called = []
    import app.scheduler as scheduler_module
    monkeypatch.setattr(
        scheduler_module, "_update_sync_state", lambda *a, **k: called.append((a, k))
    )

    asyncio.run(tasks.run_embedding_processor())

    assert called == []


def test_run_embedding_space_backfill_records_error_the_same_way(monkeypatch):
    monkeypatch.setattr(
        tasks,
        "_run_space_backfill_blocking",
        lambda: (3, {"fastembed-bge-small": 3}, "space backfill boom"),
    )

    recorded = {}

    def _fake_update_sync_state(name, status, error=None, trigger=None, **kwargs):
        recorded.update(name=name, status=status, error=error, trigger=trigger)

    import app.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "_update_sync_state", _fake_update_sync_state)

    asyncio.run(tasks.run_embedding_space_backfill())

    assert recorded["name"] == "embedding"
    assert recorded["status"] == "error"
    assert "boom" in recorded["error"]
    assert recorded["trigger"] == "embedding_space_backfill"


def test_run_embedding_processor_uses_the_module_default_constant():
    # The fallback constant used when config lookup itself fails must agree
    # with the manifest's declared default, or a config-read failure and a
    # config-read success would silently behave differently.
    assert tasks.DEFAULT_PROCESSOR_BUDGET_SECONDS == (
        MANIFEST.config_schema["processor_budget_seconds"].default
    )
    assert tasks.DEFAULT_SPACE_BACKFILL_BUDGET_SECONDS == (
        MANIFEST.config_schema["space_backfill_budget_seconds"].default
    )
