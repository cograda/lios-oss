"""The training cron's target.

`TaskSpec` cron targets are resolved as dotted refs and called with no
arguments, so each deriver declares a zero-argument wrapper like this one and
points its manifest at it. Two lines, but they are the reason training has its
own cadence instead of being folded into the prediction cycle.

Errors are logged and swallowed. A failed *training* run must not look like a
failed integration: the previously activated model is still serving, so
predictions keep working, and the failure belongs on the `AlgoRun` row (which
`AlgoIntegration.train()` has already written) rather than in the scheduler's
retry loop.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _train_blocking() -> None:
    from app.algo import train_algo

    train_algo("__ALGO_NAME__")


async def run_training() -> None:
    try:
        await asyncio.to_thread(_train_blocking)
    except Exception:
        logger.exception("__ALGO_NAME__: training run failed")
