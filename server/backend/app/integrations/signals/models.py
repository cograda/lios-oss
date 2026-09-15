"""signals' two tables — the raw inlet ledger and the per-window watch runs.

Household-shared, following `snags`/`tasks`: deliberately NO `UserOwnedMixin`.
A camera event or a watcher's verdict belongs to the house, not to whichever
user happens to be looking at the dashboard — see `tasks/models.py`'s
docstring for the same reasoning.

`SignalEvent` is the raw, unfiltered ledger of every accepted inlet hit —
`kind="unknown"` for a payload shape we don't recognise, never a rejection.
`WatchRun` is one row per watcher per night/window: opened when the window's
schedule opens, closed (`detected` or `none`) when the window ends or a
detection fires. See `watchers/base.py` for the state machine that writes it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base

KINDS = ("person", "vehicle", "package", "ring", "motion", "unknown")

WATCH_STATUSES = ("watching", "detected", "none", "error")


class SignalEvent(Base):
    """One accepted hit on `POST /api/v1/signals/{source}`.

    Every accepted request is stored, whatever its shape — an unrecognised
    payload lands here with `kind="unknown"` rather than being rejected, so
    the first real hit from a not-yet-supported device tells us its shape
    instead of vanishing as a 4xx nobody reads the logs for.
    """

    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    # Sender's device id/MAC, normalised lower-case. Nullable — a payload
    # shape we don't recognise may carry no identifiable device at all.
    device_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Resolved from config (`signals_devices`), not from the payload — a
    # human-readable name for whichever device sent this, e.g. "front_door".
    device_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The provider's own event id, if the payload carries one — lets a later
    # lookup tie this row back to Protect's own timeline. Nullable and not
    # unique: a shape we don't recognise may have none, and a provider is
    # free to resend.
    sender_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    __table_args__ = (
        Index("ix_signal_events_source_device_occurred", "source", "device_key", "occurred_at"),
    )


class WatchRun(Base):
    """One watcher's outcome for one scheduled window ("night").

    `night_date` is the LOCAL calendar date the window opened on (Europe/
    Dublin) — a window that opens 20:30 and closes 00:30 the next day still
    belongs to the evening it opened, which is how a person names "Tuesday's
    milk" even though the delivery itself may land after midnight.

    Once `status` moves to `detected`, further inlet events in the same
    window are still recorded as `SignalEvent` rows but do not re-trigger a
    vision check — see `watchers/base.py::Watcher.on_event`'s debounce.
    """

    __tablename__ = "watch_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watcher: Mapped[str] = mapped_column(String(50), nullable=False)
    night_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    baseline_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="watching")
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    frame_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    answer: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How many vision calls this window has made — the debounce means this
    # is usually 1 once detected, but every poll before that increments it.
    checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Human grading (watch_confirm). NULL = ungraded.
    confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    confirmed_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_watch_runs_watcher_night", "watcher", "night_date", unique=True),
    )
