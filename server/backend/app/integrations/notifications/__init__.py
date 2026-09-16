"""notifications — pushes system alerts via Home Assistant mobile-app push.

A `CapabilityService`: nothing is pulled from an external system, so `sync()` is
inherited as a no-op. The actual work runs on the cron `background_task`
declared in `manifest.py` (`sweep.py::run_sweep`), which is why there is no
`schedule` here — see that manifest's docstring for why a cron task rather than
a sync job, and what that means for config gating.
"""

from typing import Any

from app.integrations.notifications import tools as _tools
from app.plugin.bases import CapabilityService


class NotificationsIntegration(CapabilityService):
    @property
    def name(self) -> str:
        return "notifications"

    @property
    def display_name(self) -> str:
        return "Notifications"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return _tools.get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Open alert count for the dashboard panel.

        Cross-user by design (household infrastructure, not personal data) —
        the same admin-view stance other integrations' `dashboard_data` takes.
        """
        from app.db import get_db
        from app.integrations.notifications.models import NotificationSend

        db = get_db()
        with db.session() as session:
            open_count = (
                session.query(NotificationSend)
                .filter(NotificationSend.resolved_at.is_(None))
                .count()
            )
        return {"open_alerts": open_count}
