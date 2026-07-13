"""System integration — cross-cutting diagnostic tools.

Always configured, never syncs. Exposes tools that query across
all integrations (alerts, health checks, etc.).
"""

from typing import Any

from app.integrations.base import BaseIntegration
from app.integrations.system.tools import get_mcp_tools


class SystemIntegration(BaseIntegration):
    """Diagnostic tools that span all integrations."""

    @property
    def name(self) -> str:
        return "system"

    @property
    def display_name(self) -> str:
        return "System"

    def sync(self) -> None:
        pass  # Nothing to sync

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        return {"status": "ok"}

    def sync_schedule(self) -> str | None:
        return None  # No sync needed

    def is_configured(self) -> bool:
        return True
