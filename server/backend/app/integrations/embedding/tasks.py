"""Embedding queue processor — cron background task.

Moved out of `app.plugin.kernel_jobs.KERNEL_JOBS` (V4 chunk 3.4) into this
package's own manifest `background_tasks` — this was the one "kernel job"
that was really about a single integration's own upkeep (the embedding
queue), not cross-cutting kernel housekeeping like the audit-table prunes
that stay in `kernel_jobs.py`. Same `*/5 * * * *` cadence, same job id
("embedding_processor") as before, so `tests/test_scheduler_jobs.py`'s
pinned schedule snapshot doesn't need to change.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _run_embedding_blocking() -> None:
    """Process embedding queue in a thread (fastembed is CPU-bound)."""
    from app.db import get_db
    from app.services.embedding import EmbeddingService

    db = get_db()
    with db.session() as session:
        processed = EmbeddingService.process_queue(session, batch_size=100)
        if processed:
            logger.info(f"Embedding processor: embedded {processed} items")


async def run_embedding_processor() -> None:
    """Process the unified embedding queue."""
    try:
        await asyncio.to_thread(_run_embedding_blocking)
    except Exception:
        logger.exception("Embedding processor failed")
