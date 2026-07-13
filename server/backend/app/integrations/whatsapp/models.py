"""WhatsApp message and contact models.

Tables are created by the Node.js bridge (CREATE TABLE IF NOT EXISTS)
and also defined here so SQLAlchemy can query them. The bridge writes,
the Python side reads.

Multi-user: messages are per-user (one Baileys session = one phone number =
one user). Contacts are SHARED — the contact graph (jid → name) is global.
The migration sets server_default='1' on user_id so the existing single-user
bridge keeps working without code changes; the second container (Phase F)
will explicitly INSERT user_id=2.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin, UserOwnedMixin


class WhatsAppMessage(UserOwnedMixin, SourcedRecordMixin, Base):
    """A WhatsApp message — written by the bridge, read by the server."""

    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        UniqueConstraint("user_id", "message_id", name="uq_wa_user_msg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(100), index=True)
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    chat_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_id: Mapped[str] = mapped_column(String(100), index=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_group: Mapped[bool] = mapped_column(Boolean, default=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    message_type: Mapped[str] = mapped_column(String(20), default="text")
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_to_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # synced_at, source_id, source_ts, content_hash inherited from SourcedRecordMixin
    # Note: bridge writes NULL for mixin columns; Python backfills source_id/source_ts


class WhatsAppContact(Base):
    """A WhatsApp contact or group — written by the bridge, read by the server."""

    __tablename__ = "whatsapp_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jid: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notify_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_group: Mapped[bool] = mapped_column(Boolean, default=False)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
