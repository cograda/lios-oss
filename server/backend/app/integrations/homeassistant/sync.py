"""Sync logic: fetch HA states → upsert ha_entities + append ha_state_changes.

Also appends `ha_entity_churn` rows on first sight of an entity ("added")
and just before an entity is reconcile-deleted ("removed", with a snapshot
of its last known domain/friendly_name/state) — see `models.py::HAEntityChurn`.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.integrations.homeassistant.client import fetch_area_map, fetch_states
from app.integrations.homeassistant.models import HAEntity, HAEntityChurn, HAStateChange
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)


def _is_numeric(state: str | None) -> bool:
    if state is None:
        return False
    try:
        float(state)
        return True
    except ValueError:
        return False


def should_record_transition(
    old_state: str | None, new_state: str | None, entity_id: str | None = None
) -> bool:
    """History rule, shared by the poll sync and the WS event listener.

    Numeric→numeric ticks (temperature etc.) are skipped by default — HA's own
    recorder keeps those series, and recording them for all ~1,700 entities
    would flood `ha_state_changes`. A numeric sensor going `unavailable` (or
    back) IS recorded either way.

    Two ways to opt in, and the narrow one is almost always the right one:

    - `ha_numeric_history_entities` — a per-entity allowlist. This is what a
      deriver needs: `app.algo`'s `features()` is called with a *historical*
      timestamp during training, so a forecaster can only be trained on
      entities whose numeric history is actually kept. Four entities is a
      handful of rows a day; that is a completely different proposition from
      the flag below.
    - `ha_record_numeric_history` — the firehose, global, default off. Kept
      because it is occasionally useful for a debugging session, but turning it
      on for the fleet to feed one forecaster is the wrong trade: HA's recorder
      already holds those series, and this table would grow without anything
      reading most of it.

    `entity_id` is optional so the flag-only behaviour survives a caller that
    doesn't pass it — but both real call sites (the poll sync and the WS
    listener) do, because without it the allowlist can never match.
    """
    if not (_is_numeric(old_state) and _is_numeric(new_state)):
        return True
    cfg = plugin_config("homeassistant")
    if cfg.ha_record_numeric_history:
        return True
    return bool(entity_id) and entity_id in (cfg.ha_numeric_history_entities or [])


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
        last_updated = _parse_ts(s.get("last_updated"))

        row = existing.get(entity_id)
        if row is not None and row.state != state:
            if should_record_transition(row.state, state, entity_id):
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

        is_new = row is None
        if is_new:
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
        row.last_updated = last_updated
        row.synced_at = now

        if is_new:
            session.add(
                HAEntityChurn(
                    entity_id=entity_id,
                    event="added",
                    at=now,
                    domain=row.domain,
                    friendly_name=row.friendly_name,
                )
            )

    # Entities removed from HA (deleted devices) disappear from /api/states.
    # Log the churn row BEFORE deleting — snapshot the ORM attrs off the row
    # first, since there is nothing left to read once it's gone.
    for gone in existing.keys() - incoming_ids:
        gone_row = existing[gone]
        session.add(
            HAEntityChurn(
                entity_id=gone_row.entity_id,
                event="removed",
                at=now,
                domain=gone_row.domain,
                friendly_name=gone_row.friendly_name,
                last_state=gone_row.state,
            )
        )
        session.delete(gone_row)

    session.commit()
    logger.info(
        "Home Assistant sync complete: %d entities, %d state changes recorded",
        len(incoming_ids),
        changes,
    )
