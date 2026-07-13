"""Sync logic: fetch HA states → upsert ha_entities + append ha_state_changes."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.homeassistant.client import fetch_area_map, fetch_states
from app.integrations.homeassistant.models import HAEntity, HAStateChange

logger = logging.getLogger(__name__)


def _is_numeric(state: str | None) -> bool:
    if state is None:
        return False
    try:
        float(state)
        return True
    except ValueError:
        return False


def should_record_transition(old_state: str | None, new_state: str | None) -> bool:
    """History rule, shared by the poll sync and the WS event listener.

    Numeric→numeric ticks (temperature etc.) are skipped unless
    ha_record_numeric_history is set — HA's own recorder keeps those
    series. A numeric sensor going `unavailable` (or back) IS recorded.
    """
    if settings.ha_record_numeric_history:
        return True
    return not (_is_numeric(old_state) and _is_numeric(new_state))


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def sync_home_assistant(session: Session) -> None:
    """Fetch all entity states and upsert into Postgres.

    History rule: append a ha_state_changes row when an entity's state
    differs from the stored one — unless both old and new parse as numbers
    (temperature etc. would flood the table; HA's recorder keeps those).
    A numeric sensor going `unavailable` (or back) IS recorded.
    """
    states = fetch_states()
    if not states:
        logger.warning("Home Assistant sync: no states returned, skipping")
        return

    area_map = fetch_area_map()
    now = datetime.now(timezone.utc)
    existing = {e.entity_id: e for e in session.query(HAEntity).all()}
    incoming_ids: set[str] = set()
    changes = 0

    for s in states:
        entity_id = s.get("entity_id")
        if not entity_id:
            continue
        incoming_ids.add(entity_id)
        attrs = s.get("attributes") or {}
        state = s.get("state")
        last_changed = _parse_ts(s.get("last_changed"))

        row = existing.get(entity_id)
        if row is not None and row.state != state:
            if should_record_transition(row.state, state):
                session.add(
                    HAStateChange(
                        entity_id=entity_id,
                        old_state=row.state,
                        new_state=state,
                        changed_at=last_changed or now,
                        attributes=attrs,
                    )
                )
                changes += 1

        if row is None:
            row = HAEntity(entity_id=entity_id)
            session.add(row)

        row.domain = entity_id.split(".", 1)[0]
        row.friendly_name = attrs.get("friendly_name")
        row.area = area_map.get(entity_id)
        row.device_class = attrs.get("device_class")
        row.unit = attrs.get("unit_of_measurement")
        row.state = state
        row.attributes = attrs
        row.last_changed = last_changed
        row.synced_at = now

    # Entities removed from HA (deleted devices) disappear from /api/states.
    for gone in existing.keys() - incoming_ids:
        session.delete(existing[gone])

    session.commit()
    logger.info(
        "Home Assistant sync complete: %d entities, %d state changes recorded",
        len(incoming_ids),
        changes,
    )
