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
    synced_at: Mapped[datetime] = mapped_column(
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
