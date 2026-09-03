"""MCP tool definitions and handlers for Irish Rail — live API queries."""

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.irish_rail.client import default_station, fetch_station_data
from app.tools import CustomTool, ToolAnnotations

logger = logging.getLogger(__name__)

_NO_STATION = {
    "error": (
        "No station given and no default configured. Pass `station` (an Irish "
        "Rail station code, e.g. GSTNS), or set rail_station_code via "
        "PUT /api/integrations/irish_rail/config."
    ),
    "departures": [],
}


def _resolve_station(arguments: dict[str, Any]) -> str | None:
    """Per-request station, else the configured default, else None."""
    return (arguments.get("station") or "").strip() or default_station() or None


def handle_departures(session: Session, arguments: dict[str, Any]) -> str:
    """Upcoming departures from the configured station, optionally filtered by direction."""
    direction = arguments.get("direction")
    limit = min(int(arguments.get("limit", 20)), 50)
    station = _resolve_station(arguments)
    if not station:
        return json.dumps(_NO_STATION)

    trains = fetch_station_data(station)

    if direction:
        trains = [
            t for t in trains
            if direction.lower() in (t.get("direction") or "").lower()
        ]

    trains = trains[:limit]

    if not trains:
        return json.dumps({"message": "No upcoming departures found.", "departures": []})

    return json.dumps({
        "station": station,
        "count": len(trains),
        "departures": [_train_to_dict(t) for t in trains],
    }, indent=2)


def handle_next(session: Session, arguments: dict[str, Any]) -> str:
    """Next train in each direction — quick glance."""
    station = _resolve_station(arguments)
    if not station:
        return json.dumps(_NO_STATION)
    trains = fetch_station_data(station)

    result = {}
    for direction in ["Northbound", "Southbound"]:
        match = next(
            (t for t in trains if direction.lower() in (t.get("direction") or "").lower()),
            None,
        )
        result[direction.lower()] = _train_to_dict(match) if match else None

    return json.dumps(result, indent=2)


def _train_to_dict(t: dict) -> dict:
    return {
        "train_code": t["train_code"],
        "destination": t["destination"],
        "origin": t["origin"],
        "direction": t["direction"],
        "due_in_mins": t["due_in_mins"],
        "scheduled_departure": t["scheduled_departure"],
        "expected_departure": t["expected_departure"],
        "status": t["status"],
        "train_type": t["train_type"],
        "last_location": t["last_location"],
    }


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        CustomTool(
            name="rail_departures",
            description="Upcoming train departures from the configured station (DART/Commuter). Live data from the Irish Rail API. Optionally filter by direction (Northbound/Southbound).",
            input_schema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "description": "Filter by direction: 'Northbound' (Dublin) or 'Southbound' (Wicklow/Arklow). Optional.",
                        "enum": ["Northbound", "Southbound"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max departures to return (default 20, max 50).",
                        "default": 20,
                    },
                },
            },
            handler=handle_departures,
            annotations=ToolAnnotations(
                read_only_hint=True, idempotent_hint=True, open_world_hint=True,
            ),
            category="home",
            examples=[
                "When's the next train to Dublin?",
                "Show DART departures",
                "Trains going southbound",
            ],
        ).build(),
        CustomTool(
            name="rail_next",
            description=(
                "Next train in each direction from the configured station — quick glance. "
                "Shows the soonest Northbound (Dublin) and Southbound (Wicklow/Arklow/Gorey) "
                "departures with expected times and current status."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=handle_next,
            annotations=ToolAnnotations(
                read_only_hint=True, idempotent_hint=True, open_world_hint=True,
            ),
            category="home",
            examples=[
                "When's the next train?",
                "DART times",
            ],
        ).build(),
    ]
