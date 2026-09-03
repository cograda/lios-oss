"""System integration — cross-cutting diagnostic tools.

A `CapabilityService` (V4 chunk 4.2): no external system, always
configured, never syncs. Exposes composite tools that query across other
integrations (alerts, morning briefing, week ahead, search everything) —
strictly through their declared `app.plugin.capabilities.get_capability()`
facades, never their internals (see `tools.py`'s module docstring).
"""

from typing import Any

from app.integrations.system.tools import get_mcp_tools
from app.plugin.bases import CapabilityService


class SystemIntegration(CapabilityService):
    """Diagnostic tools that span all integrations."""

    @property
    def name(self) -> str:
        return "system"

    @property
    def display_name(self) -> str:
        return "System"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        return {"status": "ok"}

    # is_configured(): default (empty config_schema -> vacuously True).
