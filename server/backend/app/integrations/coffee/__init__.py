"""Coffee integration — coffee bag library and brew session log.

Ported from brewhaha (Next.js + Supabase). Manual sync only — coffees
and brews are user-entered via MCP tools (coffee_log, coffee_brew). The
one-shot importer at scripts/import_brewhaha.py seeds the coffees table
from coffee_collection_brewhaha.csv.
"""

import logging
from typing import Any

from sqlalchemy import func as sa_func

from app.db import get_db
from app.integrations.coffee.models import Coffee, CoffeeBrew
from app.integrations.coffee.tools import get_mcp_tools
from app.plugin.bases import CapabilityService

logger = logging.getLogger(__name__)


class CoffeeIntegration(CapabilityService):
    """No external system — coffees and brews are entirely user-entered via
    MCP tools (coffee_log, coffee_brew). `sync()` is fully inherited
    (no-op) from CapabilityService; this package is a pure tool surface
    over its own tables, not a puller of anything."""

    @property
    def name(self) -> str:
        return "coffee"

    @property
    def display_name(self) -> str:
        return "Coffee"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        db = get_db()
        with db.session() as session:
            total_coffees = session.query(sa_func.count(Coffee.id)).scalar() or 0
            current_coffees = (
                session.query(sa_func.count(Coffee.id))
                .filter(Coffee.status == "current")
                .scalar() or 0
            )
            total_brews = session.query(sa_func.count(CoffeeBrew.id)).scalar() or 0

            recent_brews = (
                session.query(CoffeeBrew)
                .order_by(CoffeeBrew.brewed_at.desc())
                .limit(5)
                .all()
            )
            top_rated = (
                session.query(Coffee)
                .filter(Coffee.rating.isnot(None))
                .order_by(Coffee.rating.desc(), Coffee.created_at.desc())
                .limit(5)
                .all()
            )

            return {
                "total_coffees": total_coffees,
                "current_coffees": current_coffees,
                "total_brews": total_brews,
                "recent_brews": [
                    {
                        "coffee_id": b.coffee_id,
                        "method": b.method,
                        "brewed_at": b.brewed_at.isoformat() if b.brewed_at else None,
                        "overall": b.overall,
                    }
                    for b in recent_brews
                ],
                "top_rated": [
                    {
                        "id": c.id,
                        "name": c.name,
                        "roaster": c.roaster,
                        "rating": c.rating,
                    }
                    for c in top_rated
                ],
            }
