"""SQLAlchemy model for cached commute solver decisions.

Household-shared (no UserOwnedMixin) — same reasoning as ha_*/weather_*: rows
are produced by the unattended scheduler with no `current_user_id()` context,
so there's no user to scope to. One append-only row per solve cycle (weekday
mornings, roughly once a minute in the 07:00-08:59 window).
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class CommuteDecision(Base):
    """One solver run. Target bus/train fields are tz-naive Dublin wall-clock
    (the solver's native representation); `decided_at` and the feed timestamps
    are tz-aware (stored as UTC, converted from Dublin at write time)."""

    __tablename__ = "commute_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    # comfortable | tight | next_train | missed | degraded | logging_only
    state: Mapped[str] = mapped_column(String(32), index=True)
    status_text: Mapped[str] = mapped_column(Text)
    leave_in_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str] = mapped_column(String(32))
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    target_bus_trip_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_bus_route: Mapped[str | None] = mapped_column(String(8), nullable=True)
    target_bus_dep_home: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    target_bus_arr_interchange: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    target_train_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_train_interchange_dep: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    target_train_dest_arr: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    # The buffer-tuning signal — predicted delay of the target bus at the interchange.
    interchange_delay_min: Mapped[float | None] = mapped_column(Float, nullable=True)

    bus_feed_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dart_feed_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bus_count: Mapped[int] = mapped_column(Integer, default=0)
    dart_count: Mapped[int] = mapped_column(Integer, default=0)

    # Whether the HA sensor push succeeded — Postgres row always commits
    # regardless (see sync.py).
    ha_pushed: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<CommuteDecision {self.decided_at} {self.state}>"
