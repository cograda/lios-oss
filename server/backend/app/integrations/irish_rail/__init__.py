"""Irish Rail integration — live DART/Commuter departures from the home station.

No caching — hits the Irish Rail API directly on each request.
Real-time data goes stale in minutes so there's no point storing it.
"""

import logging
from typing import Any

from app.config import settings
from app.integrations.base import BaseIntegration
from app.integrations.irish_rail.client import fetch_station_data
from app.integrations.irish_rail.tools import get_mcp_tools

logger = logging.getLogger(__name__)

DEFAULT_STATION = settings.rail_station_code


class IrishRailIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "irish_rail"

    @property
    def display_name(self) -> str:
        return "Irish Rail"

    def sync(self) -> None:
        """No-op — data is fetched live, not cached."""
        pass

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Fetch live departures from the home station, grouped by direction."""
        trains = fetch_station_data(DEFAULT_STATION)

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
            "station": settings.rail_station_name,
            "station_code": DEFAULT_STATION,
            "northbound": northbound[:5],
            "southbound": southbound[:5],
            "total_departures": len(trains),
        }

    def sync_schedule(self) -> str | None:
        return None  # No scheduled sync — live data

    def is_configured(self) -> bool:
        return True  # Public API, no credentials needed
