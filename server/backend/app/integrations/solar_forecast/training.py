"""The weekly training cron's target.

Errors are logged and swallowed: a failed *training* run must not read as a
failed integration. The previously activated model is still serving, so
predictions carry on, and the failure is already on an `AlgoRun` row written by
`AlgoIntegration.train()`.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _train_blocking() -> None:
    from app.algo import train_algo

    train_algo("solar_forecast")


async def run_training() -> None:
    try:
        await asyncio.to_thread(_train_blocking)
    except Exception:
        logger.exception("solar_forecast: training run failed")
