"""Integration management routes — status, manual sync triggers, history."""

import asyncio
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func

from app.db import get_db
from app.integrations import get, get_all
from app.models.tokens import SyncState, SyncHistory
from app.plugin.config_store import (
    is_integration_enabled,
    plugin_config,
    set_config_value,
    set_integration_enabled,
)
from app.plugin.validate import discover_manifests
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

    manifests = discover_manifests()

    result = []
    with db.session() as session:
        for name, integration in integrations.items():
            sync_state = session.query(SyncState).filter_by(integration=name).first()
            manifest = manifests.get(name)

            result.append({
                "name": name,
                "display_name": integration.display_name,
                "configured": integration.is_configured(),
                # V4 chunk 5.1 — enable/disable is orthogonal to configured:
                # a disabled integration can be fully configured, and vice versa.
                "enabled": is_integration_enabled(name),
                "schedule": manifest.schedule if manifest else None,
                "last_sync_at": sync_state.last_sync_at.isoformat() if sync_state and sync_state.last_sync_at else None,
                "last_sync_status": sync_state.last_sync_status if sync_state else "never",
                "last_error": sync_state.last_error if sync_state else None,
                "last_sync_duration_ms": sync_state.last_sync_duration_ms if sync_state else None,
                "consecutive_failures": sync_state.consecutive_failures if sync_state else 0,
                "next_sync_at": next_run_times.get(name),
                # V4 chunk 1.3 — manifest-sourced metadata for the dashboard.
                "version": manifest.version if manifest else None,
                "type": manifest.type if manifest else None,
                "icon": manifest.icon if manifest else None,
                "description": manifest.description if manifest else None,
            })

    return {"integrations": result}


@router.put("/{name}/enabled")
async def put_integration_enabled(name: str, request: Request):
    """Flip the enable/disable switch for an integration (V4 chunk 5.1).

    Body: `{"enabled": bool}`. Disabling skips this integration's scheduler
    jobs (sync + cron background tasks) and MCP tool registration on the
    *next* process start — it does not tear down already-running state,
    since the kernel currently only reads this at startup
    (`register_mcp_tools()`, `setup_scheduler()`, `startup_tasks.start_all()`).
    """
    manifest = discover_manifests().get(name)
    if manifest is None:
        raise HTTPException(404, f"Integration {name!r} not found")

    body = await request.json()
    if not isinstance(body, dict) or "enabled" not in body:
        raise HTTPException(400, 'Body must be {"enabled": bool}')

    enabled = bool(body["enabled"])
    set_integration_enabled(name, enabled)
    return {"status": "ok", "integration": name, "enabled": enabled}


@router.get("/{name}/tools")
async def integration_tools(name: str, hours: int = 24):
    """Tools this integration currently registers, with annotations and
    recent call counts (V4 chunk 5.1 — the detail page's Tools table).

    Call counts come from the same `tool_calls` audit trail `/system/tool-stats`
    reads (2.5) — reused here rather than duplicated, scoped to this
    integration's tool names.
    """
    from datetime import datetime, timedelta, timezone

    from app.models.tool_calls import ToolCall
    from app.plugin.registry import tool_handlers, tool_metadata

    manifest = discover_manifests().get(name)
    if manifest is None:
        raise HTTPException(404, f"Integration {name!r} not found")

    names = [n for n, (_, integ) in tool_handlers.items() if integ == name]

    hours = max(1, min(hours, 24 * 30))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    counts: dict[str, int] = {}
    if names:
        db = get_db()
        with db.session() as session:
            rows = (
                session.query(ToolCall.name, func.count(ToolCall.id))
                .filter(ToolCall.name.in_(names), ToolCall.called_at >= cutoff)
                .group_by(ToolCall.name)
                .all()
            )
            counts = {n: c for n, c in rows}

    tools = [
        {
            "name": n,
            "annotations": tool_metadata.get(n, {}).get("annotations", {}),
            "calls": counts.get(n, 0),
        }
        for n in sorted(names)
    ]
    return {"integration": name, "window_hours": hours, "tools": tools}


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

    manifest = discover_manifests().get(name)

    db = get_db()
    with db.session() as session:
        sync_state = session.query(SyncState).filter_by(integration=name).first()
        # Read every attribute INSIDE the session. `db.session()` commits on
        # exit, which expires the loaded instances, so touching an attribute
        # after the block triggers a lazy refresh on a detached instance and
        # raises DetachedInstanceError. The bug was invisible for five weeks
        # because the short-circuits below (`if sync_state and ...`) mean an
        # integration that has never synced never touches an attribute at all
        # — so the endpoint worked for exactly the integrations that had done
        # nothing, and 500'd for every one that had.
        sync = (
            {
                "last_sync_at": sync_state.last_sync_at.isoformat() if sync_state.last_sync_at else None,
                "last_sync_status": sync_state.last_sync_status,
                "last_error": sync_state.last_error,
                "last_sync_duration_ms": sync_state.last_sync_duration_ms,
                "consecutive_failures": sync_state.consecutive_failures,
            }
            if sync_state is not None
            else {
                "last_sync_at": None,
                "last_sync_status": "never",
                "last_error": None,
                "last_sync_duration_ms": None,
                "consecutive_failures": 0,
            }
        )
        history = [
            {
                "started_at": r.started_at.isoformat(),
                "status": r.status,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "trigger": r.trigger,
            }
            for r in (
                session.query(SyncHistory)
                .filter_by(integration=name)
                .order_by(SyncHistory.started_at.desc())
                .limit(50)
                .all()
            )
        ]

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
        "enabled": is_integration_enabled(name),
        "version": manifest.version if manifest else None,
        "type": manifest.type if manifest else None,
        "icon": manifest.icon if manifest else None,
        "description": manifest.description if manifest else None,
        # Flows panel (V4 chunk 5.1) — straight off the manifest, no new state.
        "reads_from": manifest.reads_from if manifest else [],
        "writes_to": manifest.writes_to if manifest else [],
        "embedding_sources": manifest.embedding_sources if manifest else [],
        "depends_on": manifest.depends_on if manifest else [],
        "provides": manifest.provides if manifest else [],
        "oauth": (
            {"provider": manifest.oauth.provider, "scopes": manifest.oauth.scopes}
            if manifest and manifest.oauth else None
        ),
        "freshness_threshold_minutes": manifest.freshness_threshold_minutes if manifest else None,
        "background_tasks": (
            [
                {"name": t.name, "kind": t.kind, "cron": t.cron}
                for t in manifest.background_tasks
            ] if manifest else []
        ),
        "schedule": manifest.schedule if manifest else None,
        "next_sync_at": next_sync_at,
        **sync,
        "history": history,
    }


@router.post("/google_mail/backfill")
async def gmail_backfill(after_date: str = "2021/01/01"):
    """Backfill Gmail history from a given date. Can take several minutes."""
    from app.integrations.google_mail.facade import FACADE as gmail
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
                count = gmail.backfill(
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
    from app.integrations.lastfm.facade import FACADE as lastfm

    db = get_db()
    with db.session() as session:
        try:
            count = lastfm.backfill(session)
            return {"status": "ok", "new_scrobbles": count}
        except Exception as e:
            logger.exception("Last.fm backfill failed")
            return {"status": "error", "message": str(e)}


@router.post("/google_mail/embed")
async def gmail_embed():
    """Embed un-embedded mail messages for semantic search. Can take a long time for initial run."""
    from app.integrations.google_mail.facade import FACADE as gmail

    db = get_db()
    with db.session() as session:
        try:
            count = gmail.embed_messages(session)
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
            # No project_tags: ingest_root applies this deployment's
            # configured default (historical_corpus.default_project_tag).
            stats = ingest_root(
                session, corpus_root,
                limit_per_type=limit_per_type,
            )
            return {"status": "ok", "stats": stats}
        except Exception as e:
            logger.exception("Historical corpus ingest failed")
            return {"status": "error", "message": str(e)}


def _mask_secret(value: str) -> str:
    """"•••" + last 4 chars — never return a secret's real value over the API."""
    if not value:
        return ""
    return f"•••{value[-4:]}" if len(value) > 4 else "•••"


@router.get("/{name}/config")
async def get_integration_config(name: str):
    """Return this integration's config_schema, with current values.

    Secrets are write-only: a set secret returns a masked "•••last4" instead
    of the real value (never the plaintext or ciphertext). Non-secret values
    are returned as-is.
    """
    manifest = discover_manifests().get(name)
    if manifest is None:
        raise HTTPException(404, f"Integration {name!r} not found")

    cfg = plugin_config(name)
    fields = {}
    for key, spec in manifest.config_schema.items():
        raw_value = getattr(cfg, key)
        display_value = _mask_secret(str(raw_value)) if spec.secret and raw_value else raw_value
        fields[key] = {
            "value": display_value,
            "type": spec.type,
            "required": spec.required,
            "secret": spec.secret,
            "description": spec.description,
            "configured": bool(raw_value) if spec.required else None,
        }

    return {"integration": name, "config": fields}


@router.put("/{name}/config")
async def put_integration_config(name: str, request: Request):
    """Upsert one or more config values for this integration.

    Body: `{"key": value, ...}` — keys not in the integration's
    `config_schema` are rejected. Secret values are Fernet-encrypted before
    being written (via `set_config_value`); this route only ever sees them
    in transit over the already UI-token-gated `/api/*` surface, never logs
    them.
    """
    manifest = discover_manifests().get(name)
    if manifest is None:
        raise HTTPException(404, f"Integration {name!r} not found")

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object of {key: value}")

    unknown = set(body.keys()) - set(manifest.config_schema.keys())
    if unknown:
        raise HTTPException(400, f"Unknown config key(s) for {name!r}: {sorted(unknown)}")

    for key, value in body.items():
        set_config_value(name, key, value)

    return {"status": "ok", "integration": name, "updated": sorted(body.keys())}
