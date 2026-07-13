"""Base integration interface. All integrations implement this."""

from abc import ABC, abstractmethod
from typing import Any


class BaseIntegration(ABC):
    """Abstract base for all service integrations.

    Each integration:
    - Syncs data from an external API into Postgres
    - Exposes MCP tools for Claude Code to query
    - Provides dashboard summary data for the web UI
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique integration name, e.g. 'google_calendar'."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name, e.g. 'Google Calendar'."""

    @abstractmethod
    def sync(self) -> None:
        """Pull latest data from external API and upsert into Postgres."""

    @abstractmethod
    def mcp_tools(self) -> list[dict[str, Any]]:
        """Return MCP tool definitions for this integration.

        Each tool dict has: name, description, inputSchema, handler.
        Tool names should be namespaced: 'integration.action'.
        """

    @abstractmethod
    async def dashboard_data(self) -> dict[str, Any]:
        """Return summary data for the web dashboard."""

    def sync_schedule(self) -> str | None:
        """Cron expression for automatic sync. None = manual only.

        Examples: '*/15 * * * *' (every 15 min), '0 * * * *' (hourly).
        """
        return None

    def is_configured(self) -> bool:
        """Return True if this integration has the required config to run.

        Override to check for required API keys, tokens, etc.
        """
        return True
