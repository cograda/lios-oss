"""SQLAlchemy model for message attachment metadata.

One row per attachment detected on a Gmail or WhatsApp message. Metadata-only
until the user gates ingestion via attachments_ingest — at which point
storage_path is populated, the file is parsed by the corpus parsers, and
historical_doc_id links to the resulting HistoricalDocument.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import UserOwnedMixin


class MessageAttachment(UserOwnedMixin, Base):
    """Attachment metadata. user_id mirrors the parent message owner — Gmail
    attachments inherit from the MailMessage, WhatsApp from WhatsAppMessage."""

    __tablename__ = "message_attachments"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "message_ref", "filename",
            name="uq_msg_attachment",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 'gmail' | 'whatsapp'
    source: Mapped[str] = mapped_column(String(20), index=True)
    # For Gmail: google_message_id; for WhatsApp: message_id (Baileys key.id)
    message_ref: Mapped[str] = mapped_column(String(200), index=True)

    filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Provenance hints shown to the user in the pending list
    sender_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    chat_or_thread: Mapped[str | None] = mapped_column(String(500), nullable=True)
    message_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    # 'pending' | 'ingested' | 'skipped' | 'failed' | 'unsupported'
    parse_status: Mapped[str] = mapped_column(
        String(20), server_default="pending", index=True,
    )
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Once downloaded + parsed
    storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    historical_doc_id: Mapped[int | None] = mapped_column(
        ForeignKey("historical_documents.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
