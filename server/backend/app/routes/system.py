"""System info routes — server health, infrastructure, uptime."""

import logging
import os
import platform
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

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
