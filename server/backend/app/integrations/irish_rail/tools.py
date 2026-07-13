"""MCP tool definitions and handlers for Irish Rail — live API queries."""

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.irish_rail.client import fetch_station_data

logger = logging.getLogger(__name__)

DEFAULT_STATION = settings.rail_station_code


def handle_departures(session: Session, arguments: dict[str, Any]) -> str:
    """Upcoming departures from the home station, optionally filtered by direction."""
    direction = arguments.get("direction")
    limit = min(int(arguments.get("limit", 20)), 50)
    station = arguments.get("station", DEFAULT_STATION)

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
    station = arguments.get("station", DEFAULT_STATION)
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
        {
            "name": "rail_departures",
            "description": f"Upcoming train departures from {settings.rail_station_name} (DART/Commuter). Live data from Irish Rail API. Optionally filter by direction (Southbound for Dublin/Bray, Northbound for Drogheda).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "description": "Filter by direction: 'Southbound' (Dublin/Bray) or 'Northbound' (Drogheda). Optional.",
                        "enum": ["Northbound", "Southbound"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max departures to return (default 20, max 50).",
                        "default": 20,
                    },
                },
            },
            "handler": handle_departures,
            "category": "home",
            "examples": [
                "When's the next train to Dublin?",
                "Show DART departures",
                "Trains to Bray",
            ],
        },
        {
            "name": "rail_next",
            "description": (
                f"Next train in each direction from {settings.rail_station_name} — quick glance. "
                "Shows the soonest Southbound (Dublin/Bray) and Northbound (Drogheda) "
                "departures with expected times and current status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "handler": handle_next,
            "category": "home",
            "examples": [
                "When's the next train?",
                "DART times",
            ],
        },
    ]
