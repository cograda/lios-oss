"""Household Domains integration — Phase B of
vault/Projects/lios/Plans/household-ops-and-loops-2026-08.md.

No external system, no scheduled sync — a `CapabilityService` (the same
shape as `snags`) serving MCP tools over its own `Domain`/`DomainCheck`
tables. `sync()` is fully inherited (no-op) from `CapabilityService`; the
manifest's own `schedule=None` already means the scheduler never calls it.
"""

from typing import Any

from app.integrations.household import tools as household_tools
from app.plugin.bases import CapabilityService


class HouseholdIntegration(CapabilityService):
    @property
    def name(self) -> str:
        return "household"

    @property
    def display_name(self) -> str:
        return "Household Domains"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return household_tools.mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from app.db import get_db
        from app.integrations.household.models import Domain

        db = get_db()
        with db.session() as session:
            names = [d.name for d in session.query(Domain).order_by(Domain.name).all()]
        return {"domain_count": len(names), "domains": names}
