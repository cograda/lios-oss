"""Commute solver integration — bus -> interchange -> rail -> destination.

The specific stops/stations are deployment config (see `routing.py`).

Ported from the standalone "Project SAM" solver (hardware/homeassistant/commute/)
into a comar server integration: the pyscript-on-HA deployment route was
retired in favour of running here, where the scheduler/Postgres/MCP registry
already exist. Decisions are stored in Postgres and best-effort pushed back
into HA as sensor.commute_* (see sync.py::_push_ha_sensors).
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.commute.models import CommuteDecision
from app.integrations.commute.sync import sync_commute
from app.integrations.commute.tools import get_mcp_tools
from app.plugin.bases import PullResult, SourceIntegration

logger = logging.getLogger(__name__)


class CommuteIntegration(SourceIntegration):
    """`SourceIntegration` per the manifest's `type="source"`, but `sync()`
    is kept as an override, not inherited (V4 chunk 4.3, batch D — judged,
    not mechanical). `sync_commute()` fetches both feeds, solves, persists,
    and best-effort-pushes to HA as one tightly-coupled unit — the solver
    genuinely needs both feeds together (bus + DART) to produce one
    decision, so there's no clean per-account fetch-then-store split the
    way google_calendar/lastfm have. `tests/test_commute.py` exercises
    `sync_commute()` directly and in depth (transient vs permanent feed
    failures, partial HA push failure, degraded decisions) — rerouting
    that logic through `SourceIntegration`'s `accounts()`/`pull()`/`store()`
    fan-out would risk behaviour it doesn't need (single account, no
    cursor) for no benefit, so this keeps the exact pre-conversion sync
    path. `pull()`/`store()` below are unused stubs required only to
    satisfy `SourceIntegration`'s abstract-method contract (same shape as
    `apple_reminders` in batch C)."""

    @property
    def name(self) -> str:
        return "commute"

    @property
    def display_name(self) -> str:
        return "Commute"

    def pull(self, account: Any, session: Session, cursor: str | None) -> PullResult:
        raise NotImplementedError("commute's sync() is overridden; pull() is never called")

    def store(self, session: Session, records: list[Any]) -> int:
        raise NotImplementedError("commute's sync() is overridden; store() is never called")

    def sync(self) -> None:
        """Fetch both feeds, solve, persist, best-effort push to HA."""
        db = get_db()
        with db.session() as session:
            sync_commute(session)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Latest decision, for the web dashboard."""
        db = get_db()
        with db.session() as session:
            latest = (
                session.query(CommuteDecision)
                .order_by(CommuteDecision.decided_at.desc())
                .first()
            )
            if latest is None:
                return {"status": "no_data"}
            return {
                "state": latest.state,
                "status_text": latest.status_text,
                "leave_in_min": latest.leave_in_min,
                "decided_at": latest.decided_at.isoformat() if latest.decided_at else None,
            }

    # Schedule: every minute, 07:00-08:57 Mon-Fri, Europe/Dublin (doesn't
    # drift with DST in the UTC container). Stops at :57 because
    # settings.commute_arrive_by defaults to "08:57" — the two crontab
    # fields can't express "run to :59 in hour 7 but only to :57 in hour 8",
    # so both hours are trimmed to :57. See manifest.py::MANIFEST.schedule /
    # schedule_timezone (single source of truth since V4 chunk 3.1).

    # is_configured(): default — True iff nta_api_key is set (the only
    # required key in this integration's config_schema; HA push stays
    # optional/best-effort).
