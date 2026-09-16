"""Embedding queue processor — cron background tasks.

Moved out of `app.plugin.kernel_jobs.KERNEL_JOBS` (V4 chunk 3.4) into this
package's own manifest `background_tasks` — this was the one "kernel job"
that was really about a single integration's own upkeep (the embedding
queue), not cross-cutting kernel housekeeping like the audit-table prunes
that stay in `kernel_jobs.py`. Same `*/5 * * * *` cadence, same job id
("embedding_processor") as before, so `tests/test_scheduler_jobs.py`'s
pinned schedule snapshot doesn't need to change.

**Two jobs as of 2026-09-07, splitting a job that used to do everything
inline into "answer fast" and "catch up overnight":**

- `run_embedding_processor` (`*/5`) drains the queue — keeps calling
  `EmbeddingService.process_queue(batch_size=100)` until it returns 0, the
  time budget elapses, or a batch raises — rather than the old one-call-per-
  tick shape, which was a ceiling of 1,200 items/hour no matter how idle the
  box was (each call measured ~10ms of the server's own time on
  production, so a multi-thousand-item backfill spent almost all of 2.5
  hours waiting on the clock). It now also writes only the **primary**
  embedding space by default (`process_queue`'s `spaces="primary"`) rather
  than every active space — measured on production, a 100-item batch was
  taking 80-130s, of which the Gemini (primary) calls were ~2s and the
  local `bge-small` fastembed subprocess was ~70-90s, i.e. the hot path was
  paying for a space nobody's queries answer from unless the primary is
  down (see `app.services.embedding`'s module docstring, points 1-2).
- `run_embedding_space_backfill` (nightly, `30 3 * * *`) is the other half:
  it walks the anti-join for every non-primary active space and fills the
  gap, reusing `backfill.fill_space` — the same walker the `fill-space` CLI
  command already used to turn a new provider on, per "Phase 4's backfill
  walks; do not write a second walker."

The 100-item batch cap itself is unchanged and stays — it's the memory
guard, not the ceiling: a 500-item untrimmed batch once took a 7.8 GB
server to a standstill on the local fastembed path. Both jobs share the
same `_drain_queue` loop shape (call a bounded unit of work repeatedly
until it's exhausted, the budget elapses, or it raises), just over
different underlying calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)

# Fallbacks if the `embedding` integration's config schema is ever
# unreachable (should not happen in production — see the try/except in
# each `_run_*_blocking` below).
DEFAULT_PROCESSOR_BUDGET_SECONDS = 240
DEFAULT_SPACE_BACKFILL_BUDGET_SECONDS = 2400


def _drain_queue(
    process_batch: Callable[[], int],
    *,
    budget_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, int, str | None]:
    """Call `process_batch()` repeatedly until it returns 0, the time
    budget elapses, or it raises.

    Returns `(total_processed, batches, error_text)`. `error_text` is
    `None` on a clean stop (drained, or budget elapsed); on a raised
    exception, this stops the loop for THIS run rather than propagating —
    the caller logs it and records it the way other cron jobs do, and the
    next tick/night retries. Earlier successful batches are still counted
    and returned even when a later one fails.

    `process_batch` is expected to do one bounded unit of work per call,
    including its own commit (see `_run_embedding_blocking` and
    `_run_space_backfill_blocking`, which each open one DB session per
    call) — this function has no DB/session concerns of its own, which is
    what makes it cheaply unit-testable with a fake.

    The budget is checked *before* each call, not after, so a run already
    at or past budget makes zero calls to `process_batch` — a call itself
    is never cut off mid-flight.
    """
    start = clock()
    total = 0
    batches = 0
    while clock() - start < budget_seconds:
        try:
            processed = process_batch()
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
            logger.exception("embedding drain loop: a batch failed")
            return total, batches, str(exc)[:500]
        if processed == 0:
            break
        total += processed
        batches += 1
    return total, batches, None


def _run_embedding_blocking() -> tuple[int, int, str | None]:
    """Drain the embedding queue in a thread (fastembed is CPU-bound).

    One DB session per batch — matching `process_queue`'s own multi-commit
    behavior (it commits the orphan-reclaim, the embed, and the cleanup
    separately) and how a hand-run drain loop worked before this change.
    Writes only the primary space per batch (`process_queue`'s
    `spaces="primary"` default) — see the module docstring.
    """
    from app.db import get_db
    from app.plugin.config_store import plugin_config
    from app.services.embedding import EmbeddingService

    try:
        budget_seconds = plugin_config("embedding").processor_budget_seconds
    except Exception:  # noqa: BLE001 - config lookup must never sink the job
        logger.exception(
            "Failed to read embedding.processor_budget_seconds — using default %s",
            DEFAULT_PROCESSOR_BUDGET_SECONDS,
        )
        budget_seconds = DEFAULT_PROCESSOR_BUDGET_SECONDS

    db = get_db()

    def _one_batch() -> int:
        with db.session() as session:
            return EmbeddingService.process_queue(session, batch_size=100)

    return _drain_queue(_one_batch, budget_seconds=budget_seconds)


async def run_embedding_processor() -> None:
    """Process the unified embedding queue, batch by batch, until it's
    empty, a batch processes nothing, or the configured time budget
    elapses — see `_drain_queue`'s docstring and the module docstring for
    why (and for the primary-space-only write policy).
    """
    total, batches, error_text = await asyncio.to_thread(_run_embedding_blocking)

    from app.services.runs import current_run

    run = current_run()
    if run is not None:
        run.touched(processed=total, batches=batches)

    if error_text:
        # Same pattern as other manifest `background_tasks` cron jobs that
        # don't sit behind a per-integration `schedule`/`sync()`
        # (`routines.run_tick`, `reminders_inlet.run_tick`): record the
        # failure on SyncState directly rather than letting it fail
        # silently, and let the next tick retry rather than raising out of
        # this coroutine.
        from app.scheduler import _update_sync_state

        _update_sync_state(
            "embedding", "error", error=error_text, trigger="embedding_processor",
        )
        return

    if batches > 1:
        logger.info(
            f"Embedding processor: embedded {total} items across {batches} batches"
        )
    elif total:
        logger.info(f"Embedding processor: embedded {total} items")


def _run_space_backfill_blocking() -> tuple[int, dict[str, int], str | None]:
    """Fill every non-primary active space's gap, in a thread.

    One `fill_space` call (`batch_size=100, limit=100`) per `_drain_queue`
    iteration, so the shared time budget is checked between pages rather
    than only once per space — a space with a huge gap can't starve the
    ones after it in the loop within a single night, since the deadline is
    tracked across the whole run and simply causes the loop to stop (the
    next night resumes from wherever the anti-join still shows a gap).
    """
    from app.db import get_db
    from app.integrations.embedding import backfill
    from app.plugin.config_store import plugin_config
    from app.services.embedding import _active_spaces

    try:
        budget_seconds = plugin_config("embedding").space_backfill_budget_seconds
    except Exception:  # noqa: BLE001 - config lookup must never sink the job
        logger.exception(
            "Failed to read embedding.space_backfill_budget_seconds — using default %s",
            DEFAULT_SPACE_BACKFILL_BUDGET_SECONDS,
        )
        budget_seconds = DEFAULT_SPACE_BACKFILL_BUDGET_SECONDS

    db = get_db()
    active = _active_spaces()

    per_space: dict[str, int] = {}
    if len(active) < 2:
        # 0 or 1 active spaces: there is no "other space" to catch up.
        return 0, per_space, None

    non_primary_ids = [provider.provider_id for provider, _vec_model in active[1:]]

    start = time.monotonic()
    total = 0
    error_text: str | None = None
    for provider_id in non_primary_ids:
        remaining = budget_seconds - (time.monotonic() - start)
        if remaining <= 0:
            break

        def _one_batch(pid: str = provider_id) -> int:
            with db.session() as session:
                result = backfill.fill_space(
                    session, pid, batch_size=100, limit=100, dry_run=False,
                )
                return result["embedded"]

        filled, _batches, err = _drain_queue(_one_batch, budget_seconds=remaining)
        per_space[provider_id] = filled
        total += filled
        if err:
            error_text = err
            break

    return total, per_space, error_text


async def run_embedding_space_backfill() -> None:
    """Nightly: fill every non-primary active space's gap against the
    primary — see the module docstring's second bullet and
    `_run_space_backfill_blocking`.
    """
    total, per_space, error_text = await asyncio.to_thread(_run_space_backfill_blocking)

    from app.services.runs import current_run

    run = current_run()
    if run is not None:
        run.touched(affected=per_space, total=total)

    if error_text:
        from app.scheduler import _update_sync_state

        _update_sync_state(
            "embedding", "error", error=error_text, trigger="embedding_space_backfill",
        )
        return

    if total:
        logger.info(f"Embedding space backfill: filled {total} vectors ({per_space})")
