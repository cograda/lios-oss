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
from sqlalchemy.orm import Session, load_only

from app.integrations.homeassistant.models import HAEntity, HAStateChange
from app.integrations.homeassistant.sync import _is_numeric
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import iso_or_none, serialize

logger = logging.getLogger(__name__)

SYNC_INTERVAL_MINUTES = 5
_UNKNOWN_STATES = {"unavailable", "unknown"}
_LOW_BATTERY_THRESHOLD = 20
_OFFLINE_LIST_CAP = 30
_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)


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
    Section("water", globs=("switch.sonoff_hydro_duo_1", "switch.sonoff_hydro_duo_2")),
    Section(
        "lights_switches",
        domains=frozenset({"light", "switch"}),
        only_states=frozenset({"on"}),
        exclude_globs=(
            "switch.frankfort_front_door_*",   # UniFi doorbell config toggles, always on
            "switch.ucgf_*",                   # UniFi NVR analytics/insights toggles
            "switch.sonoff_hydro_duo_*",       # covered by `water`
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
    # NIBE F2050/SMO S40 heat pump — hot water + heating circuit. Overlaps
    # `climate` by design (an entity may appear in multiple sections).
    Section(
        "heating_hot_water",
        globs=(
            "water_heater.smos40_hot_water",
            "climate.smos40_climate_system_s1",
            "sensor.hot_water_top_bt7_30009",
            "sensor.priority_31029",
            "sensor.current_outdoor_temperature_bt1_30002",
            "sensor.energy_log_current_power_consumption_32306",
        ),
    ),
    Section(
        "car",
        globs=(
            "sensor.polestar_2588_battery_charge_level",
            "sensor.polestar_2588_estimated_range",
            "sensor.polestar_2588_charging_status",
            "sensor.polestar_2588_estimated_charging_time_to_full",
        ),
    ),
    Section(
        "air_quality",
        globs=(
            "sensor.air_quality_v1_1_co2",
            "sensor.air_quality_v1_1_pm2_5",
            "sensor.air_quality_v1_1_voc_index",
            "sensor.shed_cam_eco2",
            "sensor.shed_cam_air_quality_index",
        ),
    ),
    # Populated once the commute solver integration is deployed.
    Section("commute", globs=("sensor.commute_*",)),
)


# ---------------------------------------------------------------------------
# Derived events — actionable transitions surfaced from ha_state_changes.
# Same "one entry, no new code" extension model as SECTIONS above.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EventRule:
    name: str
    label: str
    glob: str
    to_states: frozenset[str] = frozenset()    # empty = any change matches
    from_states: frozenset[str] = frozenset()

    def matches(self, change: HAStateChange) -> bool:
        if not fnmatch(change.entity_id, self.glob):
            return False
        old = change.old_state or ""
        new = change.new_state or ""
        if old in _UNKNOWN_STATES or new in _UNKNOWN_STATES:
            return False
        if self.to_states and new not in self.to_states:
            return False
        if self.from_states and old not in self.from_states:
            return False
        return True


EVENT_RULES: tuple[EventRule, ...] = (
    EventRule(
        "washer_finished", "Washing machine finished",
        "sensor.*washing_machine_machine_state",
        to_states=frozenset({"stop"}), from_states=frozenset({"run"}),
    ),
    EventRule(
        "dryer_finished", "Dryer finished",
        "sensor.*dryer_machine_state",
        to_states=frozenset({"stop"}), from_states=frozenset({"run"}),
    ),
    EventRule(
        "dishwasher_finished", "Dishwasher finished",
        "sensor.dishwasher_operation_state",
        to_states=frozenset({"finished"}),
    ),
    # The doorbell entity's state is a ring timestamp — every change is a ring.
    EventRule("doorbell_ring", "Doorbell rang", "event.frankfort_front_door_doorbell"),
    EventRule(
        "shed_presence", "Presence detected in shed",
        "binary_sensor.shed_cam_presence",
        to_states=frozenset({"on"}),
    ),
    EventRule(
        "car_charging_started", "Car charging started",
        "sensor.polestar_2588_charging_status",
        to_states=frozenset({"Charging"}),
    ),
    EventRule(
        "car_charging_stopped", "Car charging stopped",
        "sensor.polestar_2588_charging_status",
        from_states=frozenset({"Charging"}),
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


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_home_status(session: Session, arguments: dict[str, Any]) -> str:
    """Curated one-call snapshot of the house, grouped by SECTIONS.

    P8 (hardening-2026-08.md): this runs on every dashboard render and used
    to pull every column of every row — including the JSONB `attributes`
    blob, which nothing here reads. `Section.matches`/`_entity_brief` only
    ever touch entity_id/domain/friendly_name/area/device_class/unit/state,
    so `load_only` stops the ORM from fetching (or deserialising) anything
    else. This still visits every entity — the sectioning logic is Python
    matching over the whole fleet, not something a WHERE clause can do —
    but it stops materialising columns nobody uses.
    """
    entities = (
        session.query(HAEntity)
        .options(load_only(
            HAEntity.entity_id, HAEntity.domain, HAEntity.friendly_name,
            HAEntity.area, HAEntity.device_class, HAEntity.unit, HAEntity.state,
        ))
        .all()
    )
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
    # Battery alerts are split by *what kind of thing* holds the battery, because
    # the two classes mean opposite things (2026-08-19).
    #
    # A phone at 1% is ordinary life — someone will plug it in. A door sensor at
    # 1% is a monitoring outage about to happen silently. Reporting both through
    # one `low_battery` list meant the actionable case arrived alongside the
    # routine one, which is how an alert channel gets tuned out. Sam's iPhone
    # sat at 1% tonight next to entries that genuinely mattered.
    #
    # The split is *structural*, not a list of device names: HA's companion app
    # creates a sibling `<stem>_battery_state` (and `_charger_type`) sensor for
    # every phone/tablet/watch it manages, and nothing else does. Verified
    # against the live registry — that test picked out exactly the three personal
    # devices and left all thirteen hardware batteries alone. A name list would
    # also fail `tests/test_personalisation_guard.py`, and would need editing
    # every time a handset changes.
    all_entity_ids = {e.entity_id for e in entities}
    low_battery: list[dict] = []
    personal_device_battery: list[dict] = []
    for e in available:
        if e.device_class != "battery" or not _is_numeric(e.state):
            continue
        if float(e.state) >= _LOW_BATTERY_THRESHOLD:
            continue
        target = (
            personal_device_battery
            if _is_personal_device_battery(e.entity_id, all_entity_ids)
            else low_battery
        )
        target.append(_entity_brief(e))

    return json.dumps({
        **_freshness(session),
        "entity_count": len(entities),
        "sections": sections,
        "attention": {
            "offline_count": len(offline_all),
            "offline": offline,
            # Hardware whose battery dying costs you data.
            "low_battery": low_battery,
            # Phones/watches/tablets — informational, never an outage.
            "personal_device_battery": personal_device_battery,
        },
    }, indent=2)


# Suffixes HA's companion app appends to the *same stem* as a phone's battery
# sensor. Presence of any of these is what marks a battery as belonging to a
# person's device rather than to household hardware.
_COMPANION_SIBLING_SUFFIXES = ("_battery_state", "_charger_type", "_battery_health")

# Suffixes a battery-level entity itself may carry. Order is irrelevant — none is
# an `endswith` suffix of another (`_battery_level` ends in `_level`, not
# `_battery`), so exactly one can ever match. Verified by mutation: reversing this
# tuple changes no behaviour.
_BATTERY_SUFFIXES = ("_battery_level", "_battery_charge", "_battery")


def _is_personal_device_battery(entity_id: str, all_entity_ids: set[str]) -> bool:
    """Whether this battery belongs to a phone/watch/tablet rather than hardware.

    Detected by looking for a companion-app sibling sensor on the same stem — see
    the comment at the call site for why this is structural rather than a list of
    device names.

    Conservative on purpose: an unrecognised battery falls through to
    `low_battery`, the alerting side. Mistaking hardware for a phone would
    silence a real outage; mistaking a phone for hardware only adds noise.
    """
    if "." not in entity_id:
        return False
    stem = entity_id.split(".", 1)[1]
    for suffix in _BATTERY_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return any(
        f"sensor.{stem}{suffix}" in all_entity_ids
        for suffix in _COMPANION_SIBLING_SUFFIXES
    )


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


def handle_events(session: Session, arguments: dict[str, Any]) -> str:
    """Recent actionable events derived from raw state transitions (EVENT_RULES)."""
    hours = min(int(arguments.get("hours", 24)), 168)
    limit = min(int(arguments.get("limit", 50)), 200)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # SQL-level cap: most transitions don't match an EVENT_RULES pattern, so
    # we can't just LIMIT to `limit` rows pre-filter (would starve the
    # post-filter of candidates and under-return). limit*10 is generous
    # headroom for the match rate while still bounding the query — without
    # it this materialised every ha_state_changes row since `since` (up to
    # 7 days across 331 entities) just to Python-filter it down to `limit`.
    changes = (
        session.query(HAStateChange)
        .filter(HAStateChange.changed_at >= since)
        .order_by(HAStateChange.changed_at.desc())
        .limit(max(limit * 10, 2000))
        .all()
    )

    events: list[dict[str, Any]] = []
    for change in changes:
        for rule in EVENT_RULES:
            if rule.matches(change):
                events.append({
                    "event": rule.name,
                    "label": rule.label,
                    **_change_to_dict(change),
                })
                break
        if len(events) >= limit:
            break

    return json.dumps({
        **_freshness(session),
        "hours": hours,
        "event_count": len(events),
        "events": events,
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def handle_backfill_history(session, arguments: dict) -> str:
    """Import recorder history for the numeric-history allowlist.

    Deliberately scoped to the allowlist rather than taking an arbitrary
    entity_id: backfilling an entity whose numeric history comar is *not*
    keeping would import a series that then silently stops — one that looks
    complete and just ends. See `backfill.py::backfill_allowlisted`.
    """
    import json

    from app.integrations.homeassistant.backfill import DEFAULT_DAYS, backfill_allowlisted

    days = int(arguments.get("days", DEFAULT_DAYS))
    return json.dumps(backfill_allowlisted(session, days=days), indent=2)


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        CustomTool(
            name="ha_home_status",
            description=(
                "Current home status snapshot from Home Assistant — presence, "
                "appliances (washer/dryer/dishwasher/hob), active media, sprinkler "
                "valve, lights/switches that are on, room climate, heating/hot "
                "water (NIBE heat pump), car (Polestar battery/range/charging), "
                "indoor air quality, commute status, plus offline devices and low "
                "batteries. Cached in Postgres, synced every 5 minutes (check the "
                "stale flag)."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_home_status,
            annotations=_READ_ONLY,
            category="home",
            examples=[
                "What's the status of the house?",
                "Is the washing machine running?",
                "Are any lights on?",
            ],
        ).build(),
        CustomTool(
            name="ha_entity",
            description=(
                "Full state, attributes, and recent state transitions for specific "
                "Home Assistant entities. Use ha_entities first to discover "
                "entity ids."
            ),
            input_schema={
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
            handler=handle_entity,
            annotations=_READ_ONLY,
            category="home",
            examples=[
                "When does the dryer finish?",
                "What's the state of the sprinkler valve?",
            ],
        ).build(),
        CustomTool(
            name="ha_entities",
            description=(
                "Search/list known Home Assistant entities by domain, area, or "
                "name substring. Use this to discover entity ids and grow the "
                "home-status signal list."
            ),
            input_schema={
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
            handler=handle_entities,
            annotations=_READ_ONLY,
            category="home",
            examples=[
                "List all sensors in the kitchen",
                "What Home Assistant entities exist for the dishwasher?",
            ],
        ).build(),
        CustomTool(
            name="ha_history",
            description=(
                "State transition history for a Home Assistant entity over the "
                "last N days, with counts per state — answers questions like "
                "'how many washes this week' or 'when did the dishwasher last "
                "run'. Only non-numeric transitions are recorded (appliance "
                "cycles, switches, presence — not every temperature tick)."
            ),
            input_schema={
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
            handler=handle_history,
            annotations=_READ_ONLY,
            category="home",
            examples=[
                "How many times did we run the washing machine this week?",
                "When was the dishwasher last on?",
            ],
        ).build(),
        CustomTool(
            name="ha_events",
            description=(
                "Recent actionable home events — washer/dryer/dishwasher finished, "
                "doorbell rings, shed presence, car charging started/stopped. "
                "Derived from raw state transitions against a curated rule set "
                "(EVENT_RULES), not a raw transition dump."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours (default 24, max 168).",
                        "default": 24,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max events to return (default 50, max 200).",
                        "default": 50,
                    },
                },
            },
            handler=handle_events,
            annotations=_READ_ONLY,
            category="home",
            examples=[
                "Did the washing finish?",
                "Did anyone ring the doorbell today?",
                "Is the car charging?",
            ],
        ).build(),
        CustomTool(
            name="ha_backfill_history",
            description=(
                "Import Home Assistant recorder history for the entities in "
                "ha_numeric_history_entities into comar's ha_state_changes. Run "
                "this right after allowlisting an entity — comar only starts "
                "keeping its numeric history from that moment, but HA's recorder "
                "already has the last ~10 days, and a deriver trains on history. "
                "Idempotent: re-running tops up rather than duplicating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": (
                            "How far back to import (default 10 — HA's own "
                            "recorder retention default, so asking for more "
                            "usually returns nothing extra)."
                        ),
                        "default": 10,
                    },
                },
            },
            handler=handle_backfill_history,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                # Re-running imports only what is missing.
                idempotent_hint=True,
                open_world_hint=True,
            ),
            category="home",
            examples=["Backfill the solar sensor history from Home Assistant"],
        ).build(),
    ]
