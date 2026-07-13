"""Client authentication tokens and remote logs for HTTP API access."""

import secrets
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import UserOwnedMixin


class ClientToken(UserOwnedMixin, Base):
    __tablename__ = "client_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
        default=lambda: secrets.token_hex(32),
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    client_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # JSON snapshot of the daemon's supervised-task health, sent with each
    # heartbeat — lets the server see a stalled loop (ISS-001 split-brain)
    # instead of waiting for someone to notice stale reminders.
    task_health: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClientLog(UserOwnedMixin, Base):
    """Remote log entries shipped from client daemons."""

    __tablename__ = "client_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    level: Mapped[str] = mapped_column(String(10))  # DEBUG, INFO, WARNING, ERROR
    logger_name: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InstallCode(Base):
    """Single-use, time-limited code that lets a new machine fetch its
    personalised install script over HTTPS without prior credentials.

    Flow:
        1. Admin runs `python -m app.scripts.create_install_code --user sam
           --label sam-macbook`, which mints a `ClientToken` row and a row
           here pointing at it.
        2. The new machine hits `GET /api/install/<code>`. The route renders
           a bash installer with `(server_url, token, user, label)` baked in.
        3. Code is marked redeemed on first successful fetch (recording IP +
           timestamp). Further fetches with the same code return 410 Gone.
        4. Expires `created_at + 24h` regardless.

    The code itself is the auth boundary — no bearer header on the route.
    Lives over Tailscale only, so an over-the-shoulder leak is the realistic
    attack surface, and single-use + short TTL bounds the blast radius.

    Not user-scoped via UserOwnedMixin — these are admin-issued onboarding
    artefacts. The `user_id` column records *who the install is for*.
    """

    __tablename__ = "install_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
        # ~128 bits entropy. URL-safe.
        default=lambda: secrets.token_urlsafe(16),
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    # Token minted for this install. Lives in client_tokens; this is the FK
    # the route follows to find the bearer to bake into the script.
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("client_tokens.id", ondelete="RESTRICT"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    redeemed_from_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
