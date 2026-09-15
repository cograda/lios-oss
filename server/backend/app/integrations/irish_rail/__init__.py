"""Irish Rail integration — live DART/Commuter departures from the configured station.

No caching — hits the Irish Rail API directly on each request.
Real-time data goes stale in minutes so there's no point storing it.

`ActionIntegration` conversion (V4 chunk 4.3, batch A): no pull/store, no
cached table, tools hit the live API directly from their handlers. `sync()`
is the inherited no-op.
"""

import logging
from typing import Any

from app.integrations.irish_rail.client import (
    default_station, default_station_name, fetch_station_data,
)
from app.integrations.irish_rail.tools import get_mcp_tools
from app.plugin.bases import ActionIntegration

logger = logging.getLogger(__name__)

class IrishRailIntegration(ActionIntegration):
    @property
    def name(self) -> str:
        return "irish_rail"

    @property
    def display_name(self) -> str:
        return "Irish Rail"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Fetch live departures from the configured station, grouped by direction."""
        station_code = default_station()
        if not station_code:
            # No default station configured — the tools still work when given
            # an explicit `station`, so this panel just reports the gap rather
            # than erroring the whole dashboard summary.
            return {
                "station": None,
                "error": "rail_station_code is not configured",
                "northbound": [],
                "southbound": [],
                "total_departures": 0,
            }
        trains = fetch_station_data(station_code)

        northbound = []
        southbound = []
        for t in trains:
            entry = {
                "train_code": t["train_code"],
                "destination": t["destination"],
                "origin": t["origin"],
                "due_in_mins": t["due_in_mins"],
                "scheduled_departure": t["scheduled_departure"],
                "expected_departure": t["expected_departure"],
                "status": t["status"],
                "train_type": t["train_type"],
                "last_location": t["last_location"],
            }
            direction = (t.get("direction") or "").lower()
            if "north" in direction:
                northbound.append(entry)
            else:
                southbound.append(entry)

        return {
            "station": default_station_name(),
            "station_code": station_code,
            "northbound": northbound[:5],
            "southbound": southbound[:5],
            "total_departures": len(trains),
        }

    # is_configured(): default (empty config_schema -> vacuously True;
    # public API, no credentials needed).
