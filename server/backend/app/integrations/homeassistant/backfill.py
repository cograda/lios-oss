"""Seed `ha_state_changes` from Home Assistant's own recorder.

Why this exists: comar only starts keeping an entity's numeric history the
moment that entity is added to `ha_numeric_history_entities`. A deriver built
on `app.algo` trains on that history, so a freshly-configured forecaster would
be blind for as long as its training window — weeks — before it could fit
anything. HA's recorder, meanwhile, has been keeping those series all along.

So the first thing to do after allowlisting an entity is import what already
exists. ⚠️ Bounded by HA's `purge_keep_days` (default 10), so this buys days,
not months — enough to start, not enough to skip waiting.

Idempotent by construction: a row is written only if no row already exists for
that `(entity_id, changed_at)`. Re-running after a few days therefore tops up
rather than duplicating, which matters because the natural usage is to run it
again whenever you notice a gap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.homeassistant.client import fetch_history
from app.integrations.homeassistant.models import HAStateChange

logger = logging.getLogger(__name__)

#: HA's default recorder retention. Asking for more is harmless (you just get
#: what exists) but the default here is honest about what you can expect.
DEFAULT_DAYS = 10


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # HA is consistent about emitting offsets, but a naive timestamp compared
    # against a tz-aware column raises at query time rather than at parse time,
    # which is a much worse place to find out.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_numeric(state: str | None) -> bool:
    if state is None:
        return False
    try:
        float(state)
        return True
    except ValueError:
        return False


def backfill_entity(
    session: Session, entity_id: str, *, days: int = DEFAULT_DAYS
) -> dict:
    """Import one entity's recorder history into `ha_state_changes`.

    Only numeric states are imported. The point of a backfill is to feed a
    deriver, and `unavailable`/`unknown` rows are already captured by the live
    sync — importing them here would add rows nothing reads while making the
    inserted/skipped counts harder to interpret.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rows = fetch_history(entity_id, start, end)
    if not rows:
        return {"entity_id": entity_id, "fetched": 0, "inserted": 0, "skipped": 0}

    # One query for the whole window instead of one per row: ten days of a
    # 5-second sensor is ~170k rows, and a per-row existence check would be
    # 170k round trips.
    existing = set(
        session.scalars(
            select(HAStateChange.changed_at).where(
                HAStateChange.entity_id == entity_id,
                HAStateChange.changed_at >= start,
                HAStateChange.changed_at <= end,
            )
        ).all()
    )

    inserted = 0
    skipped = 0
    previous: str | None = None
    seen: set[datetime] = set()

    for row in rows:
        state = row.get("state")
        changed_at = _parse_ts(row.get("last_changed") or row.get("last_updated"))
        if changed_at is None or not _is_numeric(state):
            skipped += 1
            previous = state
            continue
        # `seen` guards against duplicates *within* one response, which the
        # recorder does emit when several attributes change on the same
        # timestamp — the DB constraint would not catch those (there isn't one)
        # and they would become duplicate training rows.
        if changed_at in existing or changed_at in seen:
            skipped += 1
            previous = state
            continue
        session.add(
            HAStateChange(
                entity_id=entity_id,
                old_state=previous,
                new_state=state,
                changed_at=changed_at,
                recorded_at=datetime.now(timezone.utc),
            )
        )
        seen.add(changed_at)
        previous = state
        inserted += 1

    session.commit()
    logger.info(
        f"HA backfill {entity_id}: {len(rows)} fetched, {inserted} inserted, "
        f"{skipped} skipped"
    )
    return {
        "entity_id": entity_id,
        "fetched": len(rows),
        "inserted": inserted,
        "skipped": skipped,
    }


def backfill_allowlisted(session: Session, *, days: int = DEFAULT_DAYS) -> dict:
    """Backfill every entity in `ha_numeric_history_entities`.

    The allowlist is the right unit: those are exactly the entities whose
    numeric history comar keeps going forward, so they are exactly the ones
    where a gap between "HA has it" and "comar has it" matters. Backfilling
    anything else would import history that then stops being maintained — a
    series that looks complete and silently ends.
    """
    from app.plugin.config_store import plugin_config

    entities = plugin_config("homeassistant").ha_numeric_history_entities or []
    if not entities:
        return {
            "entities": 0,
            "results": [],
            "note": (
                "ha_numeric_history_entities is empty — nothing is having its "
                "numeric history kept, so there is nothing worth backfilling. "
                "Add the entities a deriver needs first."
            ),
        }
    results = [backfill_entity(session, e, days=days) for e in entities]
    return {
        "entities": len(results),
        "inserted": sum(r["inserted"] for r in results),
        "results": results,
    }
