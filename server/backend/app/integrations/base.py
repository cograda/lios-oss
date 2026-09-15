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

    # Schedule (cron expression + optional IANA timezone) used to live here
    # as `sync_schedule()`/`sync_timezone()` overrides. V4 chunk 3.1 made the
    # integration manifest (`manifest.py::MANIFEST.schedule` /
    # `schedule_timezone`) the single source of truth — the kernel scheduler
    # reads those fields directly and no longer calls back into the ABC.

    def is_configured(self) -> bool:
        """Return True if this integration has the required config to run.

        Default (V4 chunk 3.3): true iff every `required` key in this
        integration's manifest `config_schema` resolves to a truthy value
        (DB-backed `integration_config`, falling back to the like-named
        `HomeSettings` env field during the transition period). Integrations
        with an empty `config_schema` are vacuously "configured" — same as
        the old unconditional `True` default.

        Override only when a real connectivity/liveness probe is needed
        beyond "is the key present" (e.g. `obsidian` checks the vault mount
        actually exists on disk; `google_mail` checks a token with the right
        OAuth scope is actually stored, not just that client_id/secret are
        set).
        """
        from app.plugin.config_store import is_configured_from_schema

        return is_configured_from_schema(self.name)
