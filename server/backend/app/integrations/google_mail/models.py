"""SQLAlchemy models for cached Gmail messages."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, Boolean, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from sqlalchemy import UniqueConstraint

from coglib import Base
from app.mixins import SourcedRecordMixin, UserOwnedMixin


class MailMessage(UserOwnedMixin, SourcedRecordMixin, Base):
    """Cached Gmail message metadata.

    google_message_id is unique per Google account, not globally — two users'
    Gmail accounts can theoretically issue overlapping ids. Composite uniqueness
    on (user_id, google_message_id) keeps that safe.
    """

    __tablename__ = "mail_messages"
    __table_args__ = (
        UniqueConstraint("user_id", "google_message_id", name="uq_mail_user_msg"),
        Index("ix_mail_messages_date", "date"),
        Index("ix_mail_messages_user_date", "user_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    google_message_id: Mapped[str] = mapped_column(String(255), index=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    account_email: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender: Mapped[str | None] = mapped_column(Text, nullable=True)
    to: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    labels: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    size_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # RFC 5322 Message-ID. The only key an archive (Takeout mbox) shares with
    # the API, so it is what lets an imported message and an API-fetched one be
    # recognised as the same mail. Not unique — mail legitimately duplicates.
    rfc_message_id: Mapped[str | None] = mapped_column(String(998), nullable=True, index=True)
    # Full body when it came from an archive. API-sourced rows leave this NULL
    # and still fetch bodies at embed time (sync.py::embed_messages).
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Human correspondence vs bulk (receipts, newsletters, notifications).
    # NULL = unclassified. Gates embedding, not storage.
    is_personal: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # synced_at inherited from SourcedRecordMixin

    def __repr__(self) -> str:
        return f"<MailMessage {self.subject} ({self.account_email})>"
