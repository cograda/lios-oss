"""System info routes — server health, infrastructure, uptime."""

import logging
import os
import platform
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import func, text

from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])

_start_time = time.monotonic()
_start_utc = datetime.now(timezone.utc)


@router.get("/info")
async def system_info():
    """Server system info — uptime, disk, DB size, Python version."""
    uptime_seconds = int(time.monotonic() - _start_time)

    # Disk usage
    disk = {}
    try:
        stat = os.statvfs("/")
        total_gb = (stat.f_blocks * stat.f_frsize) / (1024**3)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
        used_gb = total_gb - free_gb
        disk = {
            "total_gb": round(total_gb, 1),
            "used_gb": round(used_gb, 1),
            "free_gb": round(free_gb, 1),
            "percent_used": round((used_gb / total_gb) * 100, 1) if total_gb > 0 else 0,
        }
    except Exception:
        logger.exception("Failed to read disk usage")
        disk = {"error": "unavailable"}

    # DB size
    db_info = {}
    try:
        db = get_db()
        with db.session() as session:
            row = session.execute(
                text("SELECT pg_database_size(current_database())")
            ).scalar()
            db_info["size_mb"] = round(row / (1024**2), 1) if row else 0

            # Table row counts (approximate, fast)
            rows = session.execute(text(
                "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC"
            )).fetchall()
            db_info["tables"] = {r[0]: r[1] for r in rows}
    except Exception:
        logger.exception("Failed to read DB info")
        db_info = {"error": "unavailable"}

    return {
        "uptime_seconds": uptime_seconds,
        "started_at": _start_utc.isoformat(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "disk": disk,
        "database": db_info,
    }


@router.get("/background-tasks")
async def background_tasks():
    """Current state of every supervised startup task (V4 chunk 3.1).

    Simple in-memory registry (`app.plugin.supervisor`) — no persistence,
    reset on restart. `status` is one of "starting" / "running" /
    "restarting" / "stopped"; `restarts` counts crash-triggered restarts
    since process start.
    """
    from dataclasses import asdict

    from app.plugin.supervisor import get_task_states

    return {"tasks": [asdict(state) for state in get_task_states().values()]}


@router.get("/alerts")
async def system_alerts():
    """Active system alerts: integration health, data freshness, re-auth,
    and tool-call health (repeated failures / slow p95). Read-only wrapper
    around the `system_alerts` MCP tool so the dashboard can render the
    same payload without going through the MCP transport.
    """
    import json

    from app.integrations.system.facade import FACADE

    db = get_db()
    with db.session() as session:
        # Household view explicitly — the dashboard is an admin surface and
        # must show every integration's problems, not whichever subset the
        # ambient user context would otherwise scope down to.
        return json.loads(FACADE.alerts_household(session, {}))


@router.get("/tool-stats")
async def tool_stats(hours: int = 24):
    """Per-tool call stats over a trailing window.

    Returns the top ~20 tools by call volume, plus any tool that had at
    least one error in the window (even if it falls outside the top 20 by
    volume) — so a low-traffic but broken tool doesn't fall off the table.
    Ordered by error rate desc, then p95 duration desc.
    """
    from app.models.tool_calls import ToolCall

    hours = max(1, min(hours, 24 * 30))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    db = get_db()
    with db.session() as session:
        p50_expr = func.percentile_cont(0.5).within_group(ToolCall.duration_ms.asc())
        p95_expr = func.percentile_cont(0.95).within_group(ToolCall.duration_ms.asc())
        errors_expr = func.count(ToolCall.id).filter(ToolCall.status == "error")

        rows = (
            session.query(
                ToolCall.name,
                func.count(ToolCall.id).label("calls"),
                errors_expr.label("errors"),
                p50_expr.label("p50_duration_ms"),
                p95_expr.label("p95_duration_ms"),
            )
            .filter(ToolCall.called_at >= cutoff)
            .group_by(ToolCall.name)
            .all()
        )

    stats = [
        {
            "name": r.name,
            "calls": r.calls,
            "errors": r.errors,
            "error_rate": round(r.errors / r.calls, 4) if r.calls else 0.0,
            "p50_duration_ms": int(r.p50_duration_ms) if r.p50_duration_ms is not None else None,
            "p95_duration_ms": int(r.p95_duration_ms) if r.p95_duration_ms is not None else None,
        }
        for r in rows
    ]

    by_volume = sorted(stats, key=lambda s: s["calls"], reverse=True)[:20]
    with_errors = [s for s in stats if s["errors"] > 0]

    selected: dict[str, dict] = {s["name"]: s for s in by_volume}
    for s in with_errors:
        selected[s["name"]] = s

    result = list(selected.values())
    result.sort(key=lambda s: (-s["error_rate"], -(s["p95_duration_ms"] or 0)))

    return {
        "window_hours": hours,
        "tools": result,
    }
