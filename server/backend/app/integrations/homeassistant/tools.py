"""MCP tool definitions and handlers for Home Assistant — cached home status.

All tools read from Postgres (populated by the 5-min sync); none hit HA live.
Every response carries `synced_at` + `stale` so the model knows data age.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.integrations.homeassistant.models import HAEntity, HAStateChange
from app.tools.helpers import iso_or_none, serialize

logger = logging.getLogger(__name__)

SYNC_INTERVAL_MINUTES = 5
_UNKNOWN_STATES = {"unavailable", "unknown"}
_LOW_BATTERY_THRESHOLD = 20
_OFFLINE_LIST_CAP = 30


# ---------------------------------------------------------------------------
# Signal registry — THE extension point. Adding a home-status signal is one
# matcher line here (domain, entity glob, or device_class), not new code.
# An entity may appear in multiple sections; unavailable/unknown entities are
# excluded from all sections and surface under `attention` instead.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    name: str
    domains: frozenset[str] = frozenset()
    globs: tuple[str, ...] = ()
    device_classes: frozenset[str] = frozenset()
    only_states: frozenset[str] = frozenset()      # include only these states
    exclude_states: frozenset[str] = frozenset()   # drop these states
    exclude_globs: tuple[str, ...] = ()            # drop these entities (config/noise)

    def matches(self, entity: HAEntity) -> bool:
        selected = (
            entity.domain in self.domains
            or (entity.device_class or "") in self.device_classes
            or any(fnmatch(entity.entity_id, g) for g in self.globs)
        )
        if not selected:
            return False
        if any(fnmatch(entity.entity_id, g) for g in self.exclude_globs):
            return False
        state = entity.state or ""
        if self.only_states and state not in self.only_states:
            return False
        return state not in self.exclude_states


SECTIONS: tuple[Section, ...] = (
    Section("presence", domains=frozenset({"person"})),
    Section(
        "appliances",
        globs=(
            # SmartThings prefixes entities with the area (utility_room_...);
            # target the state/completion signals, not the energy tickers.
            "sensor.*washing_machine_machine_state",
            "sensor.*washing_machine_job_state",
            "sensor.*washing_machine_completion_time",
            "sensor.*dryer_machine_state",
            "sensor.*dryer_job_state",
            "sensor.*dryer_completion_time",
            # NEFF Home Connect (dishwasher + hob)
            "sensor.dishwasher_operation_state",
            "sensor.dishwasher_program_finish_time",
            "sensor.dishwasher_program_progress",
            "sensor.dishwasher_door",
            "binary_sensor.dishwasher_*_nearly_empty",  # salt/rinse-aid hooks
            "sensor.hob_operation_state",
        ),
    ),
    Section(
        "media",
        domains=frozenset({"media_player"}),
        exclude_states=frozenset({"off", "idle", "standby"}),
    ),
    # Just the two valve channels — the device also exposes ~20 irrigation
    # plan/config switches we don't want in a status glance.
    Section("water", globs=("switch.example_sprinkler_1", "switch.example_sprinkler_2")),
    Section(
        "lights_switches",
        domains=frozenset({"light", "switch"}),
        only_states=frozenset({"on"}),
        exclude_globs=(
            "switch.example_doorbell_*",       # UniFi doorbell config toggles, always on
            "switch.ucgf_*",                   # UniFi NVR analytics/insights toggles
            "switch.example_sprinkler_*",      # covered by `water`
            "switch.dishwasher_*",             # appliance feature switches
            "switch.hob_*",
            "switch.*washing_machine_*",
            "switch.*dryer_*",
        ),
    ),
    Section(
        "climate",
        domains=frozenset({"climate"}),
        device_classes=frozenset({"temperature", "humidity"}),
        exclude_globs=("sensor.slzb_*",),      # Zigbee coordinator chip temps
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _freshness(session: Session) -> dict[str, Any]:
    latest = session.query(func.max(HAEntity.synced_at)).scalar()
    if latest is None:
        return {"synced_at": None, "stale": True}
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - latest
    return {
        "synced_at": latest.isoformat(),
        "stale": age > timedelta(minutes=2 * SYNC_INTERVAL_MINUTES),
    }


def _entity_brief(e: HAEntity) -> dict[str, Any]:
    out = serialize(e, ["entity_id", "state", "friendly_name"])
    if e.unit:
        out["unit"] = e.unit
    if e.area:
        out["area"] = e.area
    return out


def _entity_full(e: HAEntity) -> dict[str, Any]:
    return {
        **_entity_brief(e),
        **serialize(
            e,
            ["domain", "device_class", "attributes", "last_changed", "synced_at"],
            transforms={"last_changed": iso_or_none, "synced_at": iso_or_none},
        ),
    }


def _change_to_dict(c: HAStateChange) -> dict[str, Any]:
    return serialize(
        c,
        ["entity_id", "old_state", "new_state", "changed_at"],
        transforms={"changed_at": iso_or_none},
    )


def _is_numeric(state: str | None) -> bool:
    if state is None:
        return False
    try:
        float(state)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_home_status(session: Session, arguments: dict[str, Any]) -> str:
    """Curated one-call snapshot of the house, grouped by SECTIONS."""
    entities = session.query(HAEntity).all()
    if not entities:
        return json.dumps({
            "message": "No Home Assistant data synced yet.",
            **_freshness(session),
        })

    available = [e for e in entities if (e.state or "") not in _UNKNOWN_STATES]
    sections: dict[str, list[dict]] = {}
    for section in SECTIONS:
        matched = [_entity_brief(e) for e in available if section.matches(e)]
        if matched:
            sections[section.name] = matched

    # Cap the offline list — a stranded device fleet (e.g. ESPHome boards
    # awaiting reflash) can put 100+ entities here and bloat every response.
    offline_all = [e for e in entities if (e.state or "") in _UNKNOWN_STATES]
    offline = [_entity_brief(e) for e in offline_all[:_OFFLINE_LIST_CAP]]
    low_battery = [
        _entity_brief(e)
        for e in available
        if e.device_class == "battery"
        and _is_numeric(e.state)
        and float(e.state) < _LOW_BATTERY_THRESHOLD
    ]

    return json.dumps({
        **_freshness(session),
        "entity_count": len(entities),
        "sections": sections,
        "attention": {
            "offline_count": len(offline_all),
            "offline": offline,
            "low_battery": low_battery,
        },
    }, indent=2)


def handle_entity(session: Session, arguments: dict[str, Any]) -> str:
    """Full state + attributes + recent transitions for specific entities."""
    entity_ids = arguments.get("entity_ids") or []
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    history_limit = min(int(arguments.get("history_limit", 10)), 50)

    results = []
    missing = []
    for entity_id in entity_ids:
        entity = session.query(HAEntity).filter_by(entity_id=entity_id).first()
        if entity is None:
            missing.append(entity_id)
            continue
        changes = (
            session.query(HAStateChange)
            .filter_by(entity_id=entity_id)
            .order_by(HAStateChange.changed_at.desc())
            .limit(history_limit)
            .all()
        )
        results.append({
            **_entity_full(entity),
            "recent_changes": [_change_to_dict(c) for c in changes],
        })

    return json.dumps({
        **_freshness(session),
        "entities": results,
        "not_found": missing,
    }, indent=2)


def handle_entities(session: Session, arguments: dict[str, Any]) -> str:
    """Discovery/search over known entities — how to find signal names."""
    limit = min(int(arguments.get("limit", 50)), 200)
    q = session.query(HAEntity)
    if domain := arguments.get("domain"):
        q = q.filter(HAEntity.domain == domain)
    if area := arguments.get("area"):
        q = q.filter(HAEntity.area.ilike(f"%{area}%"))
    if query := arguments.get("query"):
        pattern = f"%{query}%"
        q = q.filter(
            HAEntity.entity_id.ilike(pattern)
            | HAEntity.friendly_name.ilike(pattern)
        )
    entities = q.order_by(HAEntity.entity_id).limit(limit).all()

    return json.dumps({
        **_freshness(session),
        "count": len(entities),
        "entities": [_entity_brief(e) for e in entities],
    }, indent=2)


def handle_history(session: Session, arguments: dict[str, Any]) -> str:
    """State transitions for an entity over a period, with per-state counts."""
    entity_id = arguments.get("entity_id")
    if not entity_id:
        return json.dumps({"error": "entity_id is required"})
    days = min(int(arguments.get("days", 7)), 90)
    limit = min(int(arguments.get("limit", 100)), 500)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    changes = (
        session.query(HAStateChange)
        .filter(
            HAStateChange.entity_id == entity_id,
            HAStateChange.changed_at >= since,
        )
        .order_by(HAStateChange.changed_at.desc())
        .limit(limit)
        .all()
    )

    counts: dict[str, int] = {}
    for c in changes:
        key = c.new_state or "none"
        counts[key] = counts.get(key, 0) + 1

    return json.dumps({
        **_freshness(session),
        "entity_id": entity_id,
        "days": days,
        "transition_count": len(changes),
        "counts_by_new_state": counts,
        "transitions": [_change_to_dict(c) for c in changes],
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        {
            "name": "ha_home_status",
            "description": (
                "Current home status snapshot from Home Assistant — presence, "
                "appliances (washer/dryer/dishwasher/hob), active media, sprinkler "
                "valve, lights/switches that are on, room climate, plus offline "
                "devices and low batteries. Cached in Postgres, synced every "
                "5 minutes (check the stale flag)."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "handler": handle_home_status,
            "category": "home",
            "examples": [
                "What's the status of the house?",
                "Is the washing machine running?",
                "Are any lights on?",
            ],
        },
        {
            "name": "ha_entity",
            "description": (
                "Full state, attributes, and recent state transitions for specific "
                "Home Assistant entities. Use ha_entities first to discover "
                "entity ids."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Entity ids, e.g. ['sensor.utility_room_washing_machine_job_state'].",
                    },
                    "history_limit": {
                        "type": "integer",
                        "description": "Max recent transitions per entity (default 10, max 50).",
                        "default": 10,
                    },
                },
                "required": ["entity_ids"],
            },
            "handler": handle_entity,
            "category": "home",
            "examples": [
                "When does the dryer finish?",
                "What's the state of the sprinkler valve?",
            ],
        },
        {
            "name": "ha_entities",
            "description": (
                "Search/list known Home Assistant entities by domain, area, or "
                "name substring. Use this to discover entity ids and grow the "
                "home-status signal list."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Filter by domain, e.g. 'sensor', 'switch', 'media_player'.",
                    },
                    "area": {
                        "type": "string",
                        "description": "Filter by area name substring, e.g. 'Kitchen'.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Substring match on entity_id or friendly name.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max entities to return (default 50, max 200).",
                        "default": 50,
                    },
                },
            },
            "handler": handle_entities,
            "category": "home",
            "examples": [
                "List all sensors in the kitchen",
                "What Home Assistant entities exist for the dishwasher?",
            ],
        },
        {
            "name": "ha_history",
            "description": (
                "State transition history for a Home Assistant entity over the "
                "last N days, with counts per state — answers questions like "
                "'how many washes this week' or 'when did the dishwasher last "
                "run'. Only non-numeric transitions are recorded (appliance "
                "cycles, switches, presence — not every temperature tick)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Entity id to fetch history for.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look-back window in days (default 7, max 90).",
                        "default": 7,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max transitions to return (default 100, max 500).",
                        "default": 100,
                    },
                },
                "required": ["entity_id"],
            },
            "handler": handle_history,
            "category": "home",
            "examples": [
                "How many times did we run the washing machine this week?",
                "When was the dishwasher last on?",
            ],
        },
    ]
