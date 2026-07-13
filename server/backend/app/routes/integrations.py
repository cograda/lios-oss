"""Integration management routes — status, manual sync triggers, history."""

import asyncio
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter

from app.db import get_db
from app.integrations import get, get_all
from app.models.tokens import SyncState, SyncHistory
from app.scheduler import scheduler, _update_sync_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("/")
async def list_integrations():
    """List all registered integrations and their sync status."""
    integrations = get_all()
    db = get_db()

    # Build a map of next run times from APScheduler jobs
    next_run_times: dict[str, str | None] = {}
    try:
        for job in scheduler.get_jobs():
            if job.id.startswith("sync_"):
                integration_name = job.id.removeprefix("sync_")
                nrt = job.next_run_time
                next_run_times[integration_name] = nrt.isoformat() if nrt else None
    except Exception:
        pass  # Scheduler may not be running in tests

    result = []
    with db.session() as session:
        for name, integration in integrations.items():
            sync_state = session.query(SyncState).filter_by(integration=name).first()

            result.append({
                "name": name,
                "display_name": integration.display_name,
                "configured": integration.is_configured(),
                "schedule": integration.sync_schedule(),
                "last_sync_at": sync_state.last_sync_at.isoformat() if sync_state and sync_state.last_sync_at else None,
                "last_sync_status": sync_state.last_sync_status if sync_state else "never",
                "last_error": sync_state.last_error if sync_state else None,
                "last_sync_duration_ms": sync_state.last_sync_duration_ms if sync_state else None,
                "consecutive_failures": sync_state.consecutive_failures if sync_state else 0,
                "next_sync_at": next_run_times.get(name),
            })

    return {"integrations": result}


@router.post("/{name}/sync")
async def trigger_sync(name: str):
    """Manually trigger a sync for a specific integration."""
    integration = get(name)
    if integration is None:
        return {"error": f"Integration '{name}' not found"}

    if not integration.is_configured():
        return {"error": f"Integration '{name}' is not configured"}

    start = time.monotonic()
    try:
        # integration.sync() is a plain blocking function — bridge onto the
        # event loop so this manual-trigger request doesn't stall it.
        await asyncio.to_thread(integration.sync)
        duration_ms = int((time.monotonic() - start) * 1000)
        _update_sync_state(name, "ok", duration_ms=duration_ms, trigger="manual")
        return {"status": "ok", "message": f"Sync completed for {name}"}
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.exception(f"Sync failed for {name}")
        _update_sync_state(name, "error", str(e), duration_ms=duration_ms, trigger="manual")
        return {"status": "error", "message": str(e)}


@router.get("/{name}/history")
async def integration_history(name: str, limit: int = 50):
    """Return recent sync history for an integration."""
    integration = get(name)
    if integration is None:
        return {"error": f"Integration '{name}' not found"}

    db = get_db()
    with db.session() as session:
        rows = (
            session.query(SyncHistory)
            .filter_by(integration=name)
            .order_by(SyncHistory.started_at.desc())
            .limit(limit)
            .all()
        )
        return {
            "integration": name,
            "history": [
                {
                    "started_at": r.started_at.isoformat(),
                    "status": r.status,
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                    "trigger": r.trigger,
                }
                for r in rows
            ],
        }


@router.get("/{name}/detail")
async def integration_detail(name: str):
    """Return full detail for a single integration (status + recent history)."""
    integration = get(name)
    if integration is None:
        return {"error": f"Integration '{name}' not found"}

    db = get_db()
    with db.session() as session:
        sync_state = session.query(SyncState).filter_by(integration=name).first()
        history = (
            session.query(SyncHistory)
            .filter_by(integration=name)
            .order_by(SyncHistory.started_at.desc())
            .limit(50)
            .all()
        )

    # Next sync from scheduler
    next_sync_at = None
    try:
        job = scheduler.get_job(f"sync_{name}")
        if job and job.next_run_time:
            next_sync_at = job.next_run_time.isoformat()
    except Exception:
        pass

    return {
        "name": name,
        "display_name": integration.display_name,
        "configured": integration.is_configured(),
        "schedule": integration.sync_schedule(),
        "next_sync_at": next_sync_at,
        "last_sync_at": sync_state.last_sync_at.isoformat() if sync_state and sync_state.last_sync_at else None,
        "last_sync_status": sync_state.last_sync_status if sync_state else "never",
        "last_error": sync_state.last_error if sync_state else None,
        "last_sync_duration_ms": sync_state.last_sync_duration_ms if sync_state else None,
        "consecutive_failures": sync_state.consecutive_failures if sync_state else 0,
        "history": [
            {
                "started_at": r.started_at.isoformat(),
                "status": r.status,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "trigger": r.trigger,
            }
            for r in history
        ],
    }


@router.post("/google_mail/backfill")
async def gmail_backfill(after_date: str = "2021/01/01"):
    """Backfill Gmail history from a given date. Can take several minutes."""
    from app.integrations.google_mail.sync import backfill_mail
    from app.models.tokens import OAuthToken

    db = get_db()
    with db.session() as session:
        tokens = (
            session.query(OAuthToken)
            .filter_by(provider="google")
            .filter(OAuthToken.scopes.contains("gmail"))
            .all()
        )

        if not tokens:
            return {"error": "No Gmail accounts configured"}

        total = 0
        results = {}
        for token in tokens:
            try:
                count = backfill_mail(
                    token.account_email, session,
                    user_id=token.user_id, after_date=after_date,
                )
                total += count
                results[token.account_email] = count
            except Exception as e:
                logger.exception(f"Backfill failed for {token.account_email}")
                results[token.account_email] = f"error: {e}"

        return {"status": "ok", "new_messages": total, "by_account": results}


@router.post("/lastfm/backfill")
async def lastfm_backfill():
    """Backfill all Last.fm scrobble history. Can take several minutes."""
    from app.integrations.lastfm.sync import backfill_scrobbles

    db = get_db()
    with db.session() as session:
        try:
            count = backfill_scrobbles(session)
            return {"status": "ok", "new_scrobbles": count}
        except Exception as e:
            logger.exception("Last.fm backfill failed")
            return {"status": "error", "message": str(e)}


@router.post("/google_mail/embed")
async def gmail_embed():
    """Embed un-embedded mail messages for semantic search. Can take a long time for initial run."""
    from app.integrations.google_mail.sync import embed_messages

    db = get_db()
    with db.session() as session:
        try:
            count = embed_messages(session)
            return {"status": "ok", "new_embeddings": count}
        except Exception as e:
            logger.exception("Gmail embedding failed")
            return {"status": "error", "message": str(e)}


@router.post("/historical_corpus/ingest")
async def historical_corpus_ingest(limit_per_type: int | None = None):
    """Ingest the historical renovation corpus. Long-running; expect minutes for Tier 1, hours for PDFs.

    The corpus root is hard-coded to `/doc_corpus` (bind-mounted from the host
    via docker-compose). Caller cannot override the root — accepting an
    arbitrary path would let an authenticated UI user index any
    container-readable directory (e.g. /etc) into queryable embeddings.
    """
    from pathlib import Path
    from app.integrations.historical_corpus.ingest import ingest_root

    corpus_root = Path("/doc_corpus")
    if not corpus_root.exists():
        return {"status": "error", "message": f"corpus root {corpus_root} does not exist inside the container"}

    db = get_db()
    with db.session() as session:
        try:
            stats = ingest_root(
                session, corpus_root,
                project_tags=["renovation"],
                limit_per_type=limit_per_type,
            )
            return {"status": "ok", "stats": stats}
        except Exception as e:
            logger.exception("Historical corpus ingest failed")
            return {"status": "error", "message": str(e)}
