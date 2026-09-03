"""Client authentication tokens and remote logs for HTTP API access."""

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.auth.hashing import hash_token, token_last4
from app.mixins import UserOwnedMixin

# Sliding TTL: a token unused for this long dies. Every authenticated use
# (resolve_token_to_user) bumps expires_at back out by this amount, so a
# token in daily use never expires.
DEFAULT_TOKEN_TTL = timedelta(days=180)


class ClientToken(UserOwnedMixin, Base):
    __tablename__ = "client_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    # sha256(token) hex — the auth-relevant column. Looked up by index
    # equality; see app/auth/hashing.py for why this doesn't need an
    # additional constant-time compare on top.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
    )
    # Last 4 chars of the plaintext, kept only for admin-UI previews and
    # 401 log lines. Never used for auth decisions.
    token_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Sliding expiry — nullable so existing/admin-minted rows can opt out
    # (e.g. a deliberately non-expiring service token), but every token
    # minted via `.mint()` gets one.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    client_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # JSON snapshot of the daemon's supervised-task health, sent with each
    # heartbeat — lets the server see a stalled loop (ISS-001 split-brain)
    # instead of waiting for someone to notice stale reminders.
    task_health: Mapped[str | None] = mapped_column(Text, nullable=True)

    @classmethod
    def for_token(
        cls, *, user_id: int, token: str, label: str = "default",
        expires_at: datetime | None = None, **kwargs,
    ) -> "ClientToken":
        """Build a row from a known plaintext token (hashes it).

        Used by the mint path (below) and directly by tests that need a
        stable bearer string to authenticate with later.
        """
        return cls(
            user_id=user_id,
            token_hash=hash_token(token),
            token_last4=token_last4(token),
            label=label,
            expires_at=expires_at,
            **kwargs,
        )

    @classmethod
    def mint(
        cls, *, user_id: int, label: str = "default", **kwargs,
    ) -> tuple["ClientToken", str]:
        """Generate a fresh bearer, return (row, plaintext).

        The plaintext is never stored — the caller must hand it to the
        device/admin now; it cannot be recovered later.
        """
        plaintext = secrets.token_hex(32)
        now = datetime.now(timezone.utc)
        row = cls.for_token(
            user_id=user_id, token=plaintext, label=label,
            expires_at=now + DEFAULT_TOKEN_TTL, **kwargs,
        )
        return row, plaintext


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

    `token_plaintext` (V4 chunk 2.3): `client_tokens.token` is now hashed at
    rest, so the plaintext bearer this install will use has nowhere else to
    live between mint time and redemption (up to 24h later, over Tailscale).
    This row already carries the same single-use + short-TTL threat model as
    the code itself, so it's the least-bad place to hold it transiently —
    cleared to NULL the moment the code is redeemed (or expires).

    F4 (2026-08-08): a live bearer sitting in cleartext at rest for up to 24h
    is itself a finding, independent of the single-use/short-TTL story above
    — anyone with read access to the DB (a backup, a stray query, another
    integration's bug) gets a usable token for free. Encrypted at rest with
    the same Fernet machinery as `oauth_tokens`/`integration_config`
    (`app.auth.encryption`) — write side encrypts in
    `app/scripts/create_install_code.py`, read side decrypts in
    `app/routes/install.py`. `Text` rather than `String(64)`: Fernet
    ciphertext of a 64-char hex token is well over 64 characters.
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
    # the route follows to find the token row (for is_active/expiry checks).
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("client_tokens.id", ondelete="RESTRICT"), nullable=False,
    )
    # Transient bearer, encrypted at rest (F4) — see class docstring. NULL
    # once redeemed/expired. Read/write only via decrypt_token/encrypt_token.
    token_plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
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
