"""SQLAlchemy models for cached Home Assistant state.

Household-shared (no UserOwnedMixin) — same reasoning as weather_*.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class HAEntity(Base):
    """Latest known state per entity. Upserted each sync."""

    __tablename__ = "ha_entities"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(64), index=True)
    friendly_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    area: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_changed: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # HA's own `last_updated` — distinct from `last_changed`: HA bumps this
    # on every state write it receives (attribute-only changes included),
    # while `last_changed` only moves when the state *value* changes. Added
    # 2026-09 (issue #167) so a staleness check can ask "has HA heard
    # anything from this entity recently" rather than "did its value ever
    # change" — a sensor whose publisher has died leaves both columns
    # frozen at the same instant, which is exactly the signal that matters.
    # Nullable because rows written before this column existed have none —
    # callers must treat a missing value as "can't judge freshness", not as
    # evidence of staleness.
    last_updated: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # ⚠️ `synced_at` above is bumped on EVERY sync (`sync.py`: `row.synced_at =
    # now`), so it is a heartbeat and cannot tell you when an entity appeared.
    # `first_seen_at` is written once, on insert, and never touched again.
    #
    # Added 2026-08-19 because entity churn was unattributable by construction.
    # HA went from 1,573 to 1,717 entities in 24 hours and the only surviving
    # artefact was the count — a count can never tell you *what* changed. (The
    # cause turned out to be a Dreame robot vacuum: 289 entities, 184 of them
    # `unavailable`, mostly per-room `select`/`number` config entities. Found by
    # grouping the offline population by device-name token, not from stored
    # history, which no longer existed.)
    #
    # Note this used to be only half the picture: entities removed from HA
    # were hard-deleted below the sync loop with no trace of the removal.
    # `HAEntityChurn` (Wave 5.9) closes that gap — `sync.py` now logs a row
    # before deleting, and one on first sight of a new entity too.
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<HAEntity {self.entity_id}={self.state}>"


class HAStateChange(Base):
    """Append-only state transition history (non-numeric transitions only,
    unless ha_record_numeric_history is set).

    Fed in real time by the WebSocket event listener (events.py); the 5-min
    poll sync's change-detection doubles as gap-fill for any downtime.
    """

    __tablename__ = "ha_state_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(255), index=True)
    old_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<HAStateChange {self.entity_id} {self.old_state}→{self.new_state}>"


class HAEntityChurn(Base):
    """Append-only log of entities appearing/disappearing from HA (Wave 5.9).

    `HAEntity` is upserted-in-place: an entity that vanishes from `/api/states`
    is hard-deleted on reconcile (`sync.py`), and `first_seen_at`
    (2026-08-19, migration `d5e2b8f1a9c4`) only covers the "appeared" half —
    a *disappearance* left no trace at all, so "HA went 1,573 → 1,717 in 24h"
    was unrecoverable: the count could never say what changed, only that
    something did.

    One row per churn event, `event` in `{"added", "removed"}`. Snapshot
    columns (`domain`, `friendly_name`, `last_state`) capture what the
    entity *was* at the moment of the event — `ha_entities` no longer holds
    that once a row is deleted, and `friendly_name`/`domain` are only ever
    known at write time for an `added` row too (nothing else keeps history
    on a fresh entity's identity). `last_state` is meaningful only for
    `removed` (what it last reported before vanishing); left NULL for
    `added` rather than duplicating `HAEntity.state`, which is already the
    live source for "what is it now".

    Household-shared (no `UserOwnedMixin`) — HA is a house signal, same as
    `HAEntity`/`HAStateChange`.
    """

    __tablename__ = "ha_entity_churn"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(255), index=True)
    event: Mapped[str] = mapped_column(String(16), index=True)  # "added" | "removed"
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    domain: Mapped[str | None] = mapped_column(String(64), nullable=True)
    friendly_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<HAEntityChurn {self.event} {self.entity_id}@{self.at}>"
