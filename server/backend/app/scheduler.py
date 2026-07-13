"""APScheduler setup — registers sync jobs for each integration.

Sync and embedding work runs in a thread pool via asyncio.to_thread to avoid
blocking the event loop with CPU-bound tasks (fastembed inference, file I/O,
heavy DB queries). Includes single retry on transient failures and duration
tracking for observability.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.integrations import get_all

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

SYNC_TIMEOUT_SECONDS = 300  # 5 minutes
RETRY_DELAY_SECONDS = 30    # wait before retry on transient failure


def _update_sync_state(
    integration_name: str,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
    trigger: str = "scheduled",
) -> None:
    """Write sync result to SyncState (latest) and SyncHistory (append-only)."""
    from app.db import get_db
    from app.models.tokens import SyncState, SyncHistory

    now = datetime.now(timezone.utc)
    db = get_db()
    with db.session() as session:
        # Update current state (single row per integration)
        state = session.query(SyncState).filter_by(integration=integration_name).first()
        if not state:
            state = SyncState(integration=integration_name)
            session.add(state)
        state.last_sync_at = now
        state.last_sync_status = status
        state.last_error = error if error else None
        if duration_ms is not None:
            state.last_sync_duration_ms = duration_ms
        if status == "ok":
            state.consecutive_failures = 0
        else:
            state.consecutive_failures = (state.consecutive_failures or 0) + 1

        # Append to history log
        session.add(SyncHistory(
            integration=integration_name,
            started_at=now,
            status=status,
            duration_ms=duration_ms,
            error=error if error else None,
            trigger=trigger,
        ))
        session.commit()


def _run_sync_blocking(integration_name: str) -> None:
    """Execute integration sync in a thread (all current syncs do blocking I/O)."""
    from app.integrations import get

    integration = get(integration_name)
    if integration is None:
        raise ValueError(f"Integration {integration_name} not found")

    # integration.sync() is a plain blocking function; _try_sync bridges this
    # onto the event loop via asyncio.to_thread (see below).
    integration.sync()


async def _try_sync(integration_name: str) -> tuple[bool, str | None, int, bool]:
    """Attempt a single sync.

    Returns (success, error_msg, duration_ms, retryable). `retryable` is False
    when the failure is known-permanent (e.g. a revoked OAuth refresh token):
    retrying with the same dead credentials is pointless and costs API quota.
    The caller uses this to skip the retry-once step.
    """
    from app.errors import NeedsReauthError, PermanentError

    start = time.monotonic()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_run_sync_blocking, integration_name),
            timeout=SYNC_TIMEOUT_SECONDS,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return True, None, duration_ms, True
    except asyncio.TimeoutError:
        duration_ms = int((time.monotonic() - start) * 1000)
        return False, f"Sync timed out after {SYNC_TIMEOUT_SECONDS}s", duration_ms, True
    except NeedsReauthError as e:
        # Specifically a dead OAuth token — prefix the message so the
        # dashboard/system_alerts text reads the same as before. Logged at
        # DEBUG (not ERROR) so dead-token cycles don't spam the error log —
        # run_sync already logs the skip at INFO — but the full traceback is
        # still captured for anyone who goes looking.
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.debug(f"Sync failed for {integration_name} (needs re-auth)", exc_info=True)
        return False, f"needs re-auth: {e}", duration_ms, False
    except PermanentError as e:
        # Any other permanent failure (bad config, disabled API, non-reauth
        # 401/403, ...) — no point retrying with the same inputs. Same
        # spam-avoidance as NeedsReauthError above.
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.debug(f"Sync failed for {integration_name} (permanent)", exc_info=True)
        return False, str(e), duration_ms, False
    except Exception as e:
        # TransientError and any other (untyped) exception — treated as
        # retryable. integration.sync() is a plain blocking call now (no
        # asyncio.run() re-wrapping in between), so the real exception
        # reaches us directly; no need to walk the exception's cause chain.
        # Full traceback logged here since these are unexpected/worth investigating.
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.exception(f"Sync failed for {integration_name} (transient)")
        return False, str(e), duration_ms, True


async def run_sync(integration_name: str) -> None:
    """Run sync for a single integration with timeout, retry, and state tracking."""
    logger.info(f"Syncing {integration_name}...")

    success, error, duration_ms, retryable = await _try_sync(integration_name)

    if success:
        logger.info(f"Synced {integration_name} successfully ({duration_ms}ms)")
        _update_sync_state(integration_name, "ok", duration_ms=duration_ms)
        return

    # Skip retry for permanent failures — re-auth needed before any sync will
    # succeed. Logged at INFO not ERROR so dead-token cycles don't spam.
    if not retryable:
        logger.info(f"Sync skipped for {integration_name}: {error}")
        _update_sync_state(integration_name, "error", error, duration_ms=duration_ms)
        return

    # On retryable failure (not timeout), retry once after a delay
    if "timed out" not in (error or ""):
        logger.warning(f"Sync failed for {integration_name}: {error} — retrying in {RETRY_DELAY_SECONDS}s")
        await asyncio.sleep(RETRY_DELAY_SECONDS)
        success, error, duration_ms, retryable = await _try_sync(integration_name)
        if success:
            logger.info(f"Synced {integration_name} on retry ({duration_ms}ms)")
            _update_sync_state(integration_name, "ok", duration_ms=duration_ms)
            return
        error = f"(retry failed) {error}"

    logger.error(f"Sync failed for {integration_name}: {error}")
    _update_sync_state(integration_name, "error", error, duration_ms=duration_ms)


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


def _prune_client_logs_blocking() -> None:
    """Delete client log entries older than 30 days."""
    from datetime import timedelta

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
    from datetime import timedelta

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


def _check_whatsapp_bridge_blocking() -> None:
    """Probe the WhatsApp bridge's HTTP health endpoint.

    Writes a SyncState row keyed `whatsapp_bridge` so the alerts layer can
    distinguish "bridge dead" (no recent heartbeat) from "bridge alive but
    writes are failing" (data freshness probe says messages have stopped).
    """
    import urllib.request

    url = "http://whatsapp-bridge:3100/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            ok = 200 <= resp.status < 300
            error = None if ok else f"HTTP {resp.status}"
    except Exception as e:
        ok = False
        error = str(e)[:200]

    _update_sync_state(
        "whatsapp_bridge",
        status="ok" if ok else "error",
        error=error,
        trigger="heartbeat",
    )


async def run_whatsapp_bridge_heartbeat() -> None:
    """Heartbeat the WhatsApp bridge container (every minute)."""
    try:
        await asyncio.to_thread(_check_whatsapp_bridge_blocking)
    except Exception:
        logger.exception("WhatsApp bridge heartbeat failed")


def _run_backlog_sync_blocking() -> None:
    """Run vault backlog ↔ Reminders sync in a thread."""
    from app.config import settings
    from app.db import get_db
    from app.integrations.apple_reminders.backlog_sync import sync_backlogs

    if not settings.obsidian_vault_path:
        return

    db = get_db()
    with db.session() as session:
        result = asyncio.run(sync_backlogs(session, settings.obsidian_vault_path))
        if result.get("matched") or result.get("completed_in_vault") or result.get("new_to_reminders"):
            logger.info(f"Backlog sync: {result}")


async def run_backlog_sync() -> None:
    """Sync vault backlogs with Apple Reminders (scheduled)."""
    try:
        await asyncio.to_thread(_run_backlog_sync_blocking)
    except Exception:
        logger.exception("Backlog sync failed")


def setup_scheduler() -> None:
    """Register sync jobs for all integrations with a schedule."""
    integrations = get_all()

    for integration in integrations.values():
        schedule = integration.sync_schedule()
        if schedule and integration.is_configured():
            scheduler.add_job(
                run_sync,
                CronTrigger.from_crontab(schedule),
                args=[integration.name],
                id=f"sync_{integration.name}",
                replace_existing=True,
                misfire_grace_time=120,
                max_instances=1,
            )
            logger.info(f"Scheduled sync for {integration.name}: {schedule}")

    # Embedding queue processor — every 5 minutes
    scheduler.add_job(
        run_embedding_processor,
        CronTrigger.from_crontab("*/5 * * * *"),
        id="embedding_processor",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
    )
    logger.info("Scheduled embedding processor: */5 * * * *")

    # Backlog sync (Reminders ↔ vault tasks) — every 30 minutes
    scheduler.add_job(
        run_backlog_sync,
        CronTrigger.from_crontab("*/30 * * * *"),
        id="backlog_sync",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
    )
    logger.info("Scheduled backlog sync: */30 * * * *")

    # WhatsApp bridge heartbeat — every minute
    scheduler.add_job(
        run_whatsapp_bridge_heartbeat,
        CronTrigger.from_crontab("* * * * *"),
        id="whatsapp_bridge_heartbeat",
        replace_existing=True,
        misfire_grace_time=30,
        max_instances=1,
    )
    logger.info("Scheduled WhatsApp bridge heartbeat: * * * * *")

    # Client log pruning — daily at 3am
    scheduler.add_job(
        run_prune_client_logs,
        CronTrigger.from_crontab("0 3 * * *"),
        id="prune_client_logs",
        replace_existing=True,
        misfire_grace_time=300,
        max_instances=1,
    )
    logger.info("Scheduled client log pruning: daily at 03:00")

    # Tool call audit trail pruning — daily at 3am (same window as client_logs)
    scheduler.add_job(
        run_prune_tool_calls,
        CronTrigger.from_crontab("0 3 * * *"),
        id="prune_tool_calls",
        replace_existing=True,
        misfire_grace_time=300,
        max_instances=1,
    )
    logger.info("Scheduled tool_calls pruning: daily at 03:00")

    scheduler.start()
    logger.info("Scheduler started")
