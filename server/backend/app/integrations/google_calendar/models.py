"""SQLAlchemy models for cached calendar events."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin


class CalendarEvent(SourcedRecordMixin, Base):
    """Cached Google Calendar event."""

    __tablename__ = "calendar_events"
    __table_args__ = (
        Index("ix_calendar_events_start_time", "start_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    google_event_id: Mapped[str] = mapped_column(String(255), unique=True)
    calendar_account: Mapped[str] = mapped_column(String(255))  # Email of the account
    calendar_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    # synced_at inherited from SourcedRecordMixin

    def __repr__(self) -> str:
        return f"<CalendarEvent {self.summary} ({self.calendar_account})>"
