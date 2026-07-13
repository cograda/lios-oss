"""OAuth token, sync state, and sync history models."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import UserOwnedMixin


class OAuthToken(UserOwnedMixin, Base):
    """Stored OAuth tokens for Google services and other providers.

    Encrypted at rest via app/auth/encryption.py. Multi-user via UserOwnedMixin
    — each user's google account is a separate row. Sync jobs iterate this
    table row-by-row, so per-user scoping happens automatically downstream.
    """

    __tablename__ = "oauth_tokens"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", "account_email",
            name="uq_oauth_user_provider_account",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))  # google, banking
    account_email: Mapped[str] = mapped_column(String(255))
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(String(50), default="Bearer")
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)  # Space-separated
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the refresh token is revoked or rejected by the provider. Cleared
    # by a successful OAuth code exchange. Scheduler skips syncs while this is set.
    needs_reauth_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    needs_reauth_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<OAuthToken {self.provider}:{self.account_email}>"


class SyncState(Base):
    """Tracks the last sync time and status for each integration."""

    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    integration: Mapped[str] = mapped_column(String(50), unique=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_sync_status: Mapped[str] = mapped_column(String(20), default="never")  # ok, error, never
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SyncHistory(Base):
    """Append-only log of every sync attempt for trending and debugging."""

    __tablename__ = "sync_history"
    __table_args__ = (
        Index("ix_sync_history_integration_started", "integration", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    integration: Mapped[str] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20))  # ok, error, timeout
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled, manual
