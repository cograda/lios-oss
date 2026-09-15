"""signals — generic camera/sensor event inlet + watcher framework.

First source: `protect` (UniFi Protect's Alarm Manager webhook). First
watcher: the milk delivery watch. See `manifest.py` and the design note at
`vault/Projects/lios/Plans/2026-09-11 Signals — camera events, watchers and
the milk watcher.md`.

A `CapabilityService`, like `tasks`: nothing here is polled on a schedule in
the source/sync sense — UniFi Protect pushes to `routes.py`'s inlet, and the
watcher tick (`runner.run_tick`) is a `background_tasks` cron entry, exactly
the shape `tasks_routines_tick` already uses.
"""

from typing import Any

from app.plugin.bases import CapabilityService


class SignalsIntegration(CapabilityService):
    @property
    def name(self) -> str:
        return "signals"

    @property
    def display_name(self) -> str:
        return "Signals"

    def mcp_tools(self) -> list[dict[str, Any]]:
        from app.integrations.signals import tools as signals_tools

        return signals_tools.mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from sqlalchemy import func as sa_func

        from app.db import get_db
        from app.integrations.signals.models import WatchRun

        db = get_db()
        with db.session() as session:
            by_status = dict(
                session.query(WatchRun.status, sa_func.count(WatchRun.id))
                .group_by(WatchRun.status).all()
            )
        return {"runs_by_status": by_status}
