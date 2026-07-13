"""Snag register models — the database IS the source of truth.

The vault file (Household/Renovation/Snags.md) is a generated view, re-rendered
after every write. Each snag carries an immutable human-facing UID (SNAG-0042)
that is quoted to trades and stable across renames/merges.

Household-shared tables (like finance): deliberately NO UserOwnedMixin — the
house has one snag list regardless of who reports or triages.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base

TRADES = ("windowco", "painter", "ken-fergal", "plumber", "electrician", "other", "unknown")
SEVERITIES = ("critical", "major", "minor", "cosmetic")
STATUSES = (
    "open", "reported", "accepted", "disputed",
    "fixed", "verified", "closed", "wont-fix",
)


class Snag(Base):
    __tablename__ = "snags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Human-facing stable ID, e.g. 'SNAG-0042'. Assigned once, never reused.
    uid: Mapped[str] = mapped_column(String(20), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    room: Mapped[str] = mapped_column(String(100), index=True)
    element: Mapped[str | None] = mapped_column(String(200), nullable=True)

    trade: Mapped[str] = mapped_column(String(30), server_default="unknown", index=True)
    severity: Mapped[str] = mapped_column(String(20), server_default="minor", index=True)
    status: Mapped[str] = mapped_column(String(20), server_default="open", index=True)

    reported_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    # Provenance: originating message id(s), space-joined (display/debug only —
    # idempotency lives in snag_source_messages).
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Follow-up lifecycle
    reported_to_trade_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class SnagMedia(Base):
    """Evidence link: snag ↔ media store item. vault_path is set once the
    file has been exported into the vault (Attachments/Snags/UID-n.ext)."""

    __tablename__ = "snag_media"
    __table_args__ = (
        UniqueConstraint("snag_id", "media_item_id", name="uq_snag_media"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snag_id: Mapped[int] = mapped_column(
        ForeignKey("snags.id", ondelete="CASCADE"), index=True,
    )
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="RESTRICT"), index=True,
    )
    vault_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class SnagSourceMessage(Base):
    """Which raw messages have already been captured into snags — makes
    snag_capture idempotent across repeated runs (text-only messages have no
    media_items row, so snag_media alone can't dedupe them)."""

    __tablename__ = "snag_source_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_ref: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    snag_id: Mapped[int] = mapped_column(
        ForeignKey("snags.id", ondelete="CASCADE"), index=True,
    )
