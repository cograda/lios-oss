"""APScheduler setup — registers sync jobs for each integration.

Sync and embedding work runs in a thread pool via asyncio.to_thread to avoid
blocking the event loop with CPU-bound tasks (fastembed inference, file I/O,
heavy DB queries). Includes single retry on transient failures and duration
tracking for observability.

V4 chunk 3.1: job registration is manifest-driven. `setup_scheduler()`
schedules three kinds of job, all read from manifests/kernel declarations —
no integration-specific names appear in this file:

  1. Per-integration sync jobs, from each manifest's `schedule` /
     `schedule_timezone` (replaces calling the old `sync_schedule()` ABC
     method, deleted along with `sync_timezone()` now that manifests are
     the single source of truth).
  2. Kernel-owned jobs (`app.plugin.kernel_jobs.KERNEL_JOBS`) — embedding
     processor, audit-table prunes. Declared there, iterated here.
  3. Cron-kind `background_tasks` from manifests (e.g. the WhatsApp bridge
     heartbeat, the apple_reminders backlog sync) — resolved via
     `app.plugin.refs.resolve_ref` and scheduled the same way.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.integrations import get_all
from app.plugin.config_store import is_integration_enabled
from app.plugin.kernel_jobs import KERNEL_JOBS
from app.plugin.refs import resolve_ref
from app.plugin.validate import discover_manifests

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


def _wrap_scheduled_job(name: str, func, *, ledger: bool = True):
    """Wrap a scheduled-job callable so every execution writes a `runs`
    ledger row (S5.1), with zero per-job edits — see `app/services/runs.py`.

    All three registration sites below (per-integration sync, kernel jobs,
    manifest cron background tasks) funnel through here, so a job gets a
    `runs` row purely from being registered, not from its own body doing
    anything. `name` becomes both the ledger's `name` column and the value
    a job's own body can attach `touched` detail to via
    `app.services.runs.current_run()` — see `brief.py::run_prewarm` and
    `routines.py::run_tick` for the two named first tenants that do.

    Deliberately thin: this only observes whether the wrapped callable
    raised. Most jobs here (`run_sync`, the kernel prune jobs, `run_tick`,
    `run_prewarm`) already catch their own failures internally and record
    them elsewhere (`SyncState`), so most `runs` rows read `ok` even when
    the job's own work failed — that's an accepted limitation of a
    zero-edit generic wrap, not a bug; the two named tenants report their
    own findings into `touched` for exactly this reason.

    `ledger=False` (Wave 5.11, from a manifest `TaskSpec.ledger`) skips the
    `runs` insert entirely — for a job whose own liveness is already
    tracked elsewhere (a heartbeat writing its own `SyncState` row every
    run), a ledger row is pure duplication: 1,440 identical `ok` rows a day
    from `whatsapp_bridge_heartbeat` was most of `recent_runs`' payload and
    none of its information. The job still runs and still raises normally
    on failure — only the audit-trail insert is skipped.
    """
    import functools

    if not ledger:
        @functools.wraps(func)
        async def _wrapped_unledgered(*args, **kwargs):
            return await func(*args, **kwargs)

        return _wrapped_unledgered

    from app.services.runs import record_run

    @functools.wraps(func)
    async def _wrapped(*args, **kwargs):
        with record_run("scheduled_job", name, trigger="schedule"):
            return await func(*args, **kwargs)

    return _wrapped


def setup_scheduler() -> None:
    """Register sync jobs, kernel jobs, and manifest cron background tasks."""
    integrations = get_all()
    manifests = discover_manifests()

    # 1. Per-integration sync jobs, schedule sourced from the manifest.
    for name, integration in integrations.items():
        manifest = manifests.get(name)
        schedule = manifest.schedule if manifest else None
        if schedule and integration.is_configured() and is_integration_enabled(name):
            tz = manifest.schedule_timezone
            job_id = f"sync_{name}"
            scheduler.add_job(
                _wrap_scheduled_job(job_id, run_sync),
                CronTrigger.from_crontab(schedule, timezone=tz),
                args=[name],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=120,
                max_instances=1,
            )
            logger.info(f"Scheduled sync for {name}: {schedule} ({tz or 'UTC'})")

    # 2. Kernel-owned jobs (embedding processor, audit prunes).
    for job in KERNEL_JOBS:
        scheduler.add_job(
            _wrap_scheduled_job(job.id, job.func),
            CronTrigger.from_crontab(job.cron),
            id=job.id,
            replace_existing=True,
            misfire_grace_time=job.misfire_grace_time,
            max_instances=1,
        )
        logger.info(f"Scheduled kernel job {job.id}: {job.cron}")

    # 3. Cron-kind background tasks declared by manifests (e.g. WhatsApp
    #    bridge heartbeat, apple_reminders backlog sync).
    for name, manifest in manifests.items():
        if not is_integration_enabled(name):
            continue
        for task in manifest.background_tasks:
            if task.kind != "cron":
                continue
            func = resolve_ref(task.target)
            scheduler.add_job(
                _wrap_scheduled_job(task.name, func, ledger=task.ledger),
                CronTrigger.from_crontab(task.cron),
                id=task.name,
                replace_existing=True,
                misfire_grace_time=task.misfire_grace_time,
                max_instances=1,
            )
            logger.info(f"Scheduled background task {task.name} ({name}): {task.cron}")

    scheduler.start()
    logger.info("Scheduler started")
