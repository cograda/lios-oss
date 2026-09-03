"""Kernel-owned scheduled jobs — V4 chunk 3.1.

Jobs the kernel itself owns (not any integration): the daily audit-table
prunes (client_logs, tool_calls, auth_events). These aren't per-integration
sync jobs — they're kernel housekeeping — so they don't belong in any
manifest. `KERNEL_JOBS` is the single declarative list
`app.scheduler.setup_scheduler()` iterates over, replacing what used to be
five separate inline `scheduler.add_job(...)` calls (the WhatsApp bridge
heartbeat moved into the whatsapp integration's own manifest as a cron
`background_tasks` entry; apple_reminders backlog sync moved the same way;
the embedding queue processor — originally kept here alongside these because
chunk 3.1 landed before the `embedding` package existed — moved into
`app.integrations.embedding`'s own manifest `background_tasks` in chunk 3.4,
once embedding became a real capability package with its own manifest to put
it in).

Byte-identical cadences and ids to the pre-3.1 scheduler.py — see
`tests/test_scheduler_jobs.py`'s pinned snapshot (the embedding_processor
job id/cron there is now sourced from the embedding manifest instead of this
list, but the pinned schedule is unchanged).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KernelJob:
    id: str
    func: Callable[[], Awaitable[None]]
    cron: str
    misfire_grace_time: int = 120


def _prune_client_logs_blocking() -> None:
    """Delete client log entries older than 30 days."""
    from app.db import get_db
    from app.models.clients import ClientLog

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    db = get_db()
    with db.session() as session:
        deleted = (
            session.query(ClientLog)
            .filter(ClientLog.logged_at < cutoff)
            .delete(synchronize_session=False)
        )
        session.commit()
        if deleted:
            logger.info(f"Pruned {deleted} client log entries older than 30 days")


async def run_prune_client_logs() -> None:
    """Prune old client logs (runs daily)."""
    try:
        await asyncio.to_thread(_prune_client_logs_blocking)
    except Exception:
        logger.exception("Client log pruning failed")


def _prune_tool_calls_blocking() -> None:
    """Delete tool_calls audit rows older than 30 days.

    Same retention window and same daily-3am cadence as `client_logs` — both
    are append-only per-call/per-request audit trails with no long-term
    analytical value beyond system_alerts' rolling 1h/24h lookback windows.
    """
    from app.db import get_db
    from app.models.tool_calls import ToolCall

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    db = get_db()
    with db.session() as session:
        deleted = (
            session.query(ToolCall)
            .filter(ToolCall.called_at < cutoff)
            .delete(synchronize_session=False)
        )
        session.commit()
        if deleted:
            logger.info(f"Pruned {deleted} tool_calls entries older than 30 days")


async def run_prune_tool_calls() -> None:
    """Prune old tool_calls audit rows (runs daily)."""
    try:
        await asyncio.to_thread(_prune_tool_calls_blocking)
    except Exception:
        logger.exception("Tool call pruning failed")


def _prune_auth_events_blocking() -> None:
    """Delete auth_events rows older than 90 days.

    Longer retention than tool_calls/client_logs (30 days) — auth_events is
    a security audit trail (401s, token issue/revoke), reviewed less often
    but wanted for longer when it is. Same daily-3am cadence, same
    prune-job shape (V4 chunk 2.5).
    """
    from app.db import get_db
    from app.models.auth_events import AuthEvent

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    db = get_db()
    with db.session() as session:
        deleted = (
            session.query(AuthEvent)
            .filter(AuthEvent.ts < cutoff)
            .delete(synchronize_session=False)
        )
        session.commit()
        if deleted:
            logger.info(f"Pruned {deleted} auth_events entries older than 90 days")


async def run_prune_auth_events() -> None:
    """Prune old auth_events audit rows (runs daily)."""
    try:
        await asyncio.to_thread(_prune_auth_events_blocking)
    except Exception:
        logger.exception("Auth event pruning failed")


def _score_algo_predictions_blocking() -> None:
    """Grade every deriver's due predictions against what actually happened.

    Kernel-owned rather than per-integration on purpose. Scoring is the one
    part of a deriver that is identical for all of them, and making it the
    kernel's job means a new deriver is graded from its first prediction
    without declaring anything — the alternative (every deriver remembering to
    schedule its own scoring cron) fails silently, and a silent scoring outage
    looks exactly like a working forecaster.

    Hourly, not daily: a one-hour-horizon prediction is observable within the
    hour, and the sooner a row is scored the shorter the window in which a
    broken model looks fine. One deriver's failure never stops the others —
    each is scored in its own try block, because a forecaster whose `observe()`
    is broken must not stop the rest of the fleet being graded.
    """
    from app.algo.base import AlgoIntegration
    from app.db import get_db
    from app.integrations import get_all

    derivers = {
        name: integration
        for name, integration in get_all().items()
        if isinstance(integration, AlgoIntegration)
    }
    if not derivers:
        return

    db = get_db()
    for name, integration in derivers.items():
        try:
            with db.session() as session:
                result = integration.run_score(session)
            if result.get("scored") or result.get("abandoned"):
                logger.info(f"Scored {name}: {result}")
        except Exception:
            logger.exception(f"Scoring failed for deriver {name}")


async def run_score_algo_predictions() -> None:
    """Score deriver predictions against observed reality (runs hourly)."""
    try:
        await asyncio.to_thread(_score_algo_predictions_blocking)
    except Exception:
        logger.exception("Algo prediction scoring failed")


KERNEL_JOBS: list[KernelJob] = [
    KernelJob(id="prune_client_logs", func=run_prune_client_logs, cron="0 3 * * *", misfire_grace_time=300),
    KernelJob(id="prune_tool_calls", func=run_prune_tool_calls, cron="0 3 * * *", misfire_grace_time=300),
    KernelJob(id="prune_auth_events", func=run_prune_auth_events, cron="0 3 * * *", misfire_grace_time=300),
    # Twelve past the hour, not on the hour: every other cron in this system
    # fires on :00, and scoring reads tables those jobs are writing.
    KernelJob(id="score_algo_predictions", func=run_score_algo_predictions, cron="12 * * * *", misfire_grace_time=300),
]
