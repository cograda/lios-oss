"""alerts — the Alertmanager webhook inlet + reviewable alert log (lios#230).

A `CapabilityService`, like `signals`: nothing here is polled on a schedule
in the source/sync sense — Alertmanager pushes to `routes.py`'s inlet.
Registers no MCP tools of its own (`mcp_tools()` returns `[]`, same as
`sheets`/`transcription`/`vision`) — the read side is surfaced as
`system_alert_log` via the `alerts.query` capability instead, because that
is where the daily brief/kickoff already composes cross-integration reads.
"""

from typing import Any

from app.plugin.bases import CapabilityService


class AlertsIntegration(CapabilityService):
    @property
    def name(self) -> str:
        return "alerts"

    @property
    def display_name(self) -> str:
        return "Alerts"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return []

    async def dashboard_data(self) -> dict[str, Any]:
        from app.db import get_db
        from app.integrations.alerts.service import recent_event_count

        db = get_db()
        with db.session() as session:
            return {"event_count": recent_event_count(session)}
