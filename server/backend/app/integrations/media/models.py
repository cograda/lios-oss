"""SQLAlchemy model for the WhatsApp media store index.

One row per media message (image / video / audio). The scan indexes metadata
for ALL media ever seen (cheap, DB-only); the store step downloads recent
items into MEDIA_ROOT and records where each file lives. Older items are
marked 'expired' at scan time (WhatsApp CDN + sender re-upload both rot after
~3-4 weeks) but remain attemptable on demand via media_fetch.

Documents are deliberately NOT tracked here — they belong to the attachments
integration (parse-and-embed pipeline). This table is the binary store.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import UserOwnedMixin


class MediaItem(UserOwnedMixin, Base):
    """Index row for one WhatsApp media message. user_id mirrors the parent
    message owner (one Baileys session = one phone = one user)."""

    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "message_ref", name="uq_media_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 'whatsapp' (only source today; gmail could join later)
    source: Mapped[str] = mapped_column(String(20), index=True)
    # Baileys key.id — also the bridge /download/{message_ref} key
    message_ref: Mapped[str] = mapped_column(String(200), index=True)

    # 'image' | 'video' | 'audio'
    media_type: Mapped[str] = mapped_column(String(20), index=True)
    mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)

    sender_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    chat_or_thread: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    message_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    # 'indexed'  — metadata only, queued for auto-download (recent window)
    # 'stored'   — bytes on disk at storage_path
    # 'expired'  — older than the recoverable window at scan time; media_fetch
    #              may still succeed via WhatsApp re-upload, so not terminal
    # 'failed'   — download attempted and errored (skip_reason says why)
    status: Mapped[str] = mapped_column(String(20), server_default="indexed", index=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Once downloaded
    storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
