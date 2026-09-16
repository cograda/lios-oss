"""Dashboard sign-in sessions (2026-09-06 — one credential: the per-user bearer).

The admin dashboard used to authenticate with a shared password
(`HOME_UI_TOKEN`) held in a cookie. It now signs a PERSON in: the browser
posts their per-user bearer once (`POST /api/auth/login`), the server
resolves it to a `client_tokens` row and a `User`, and hands back a cookie
holding a random **session id** — never the bearer itself. A stolen cookie is
therefore a dashboard session that can be revoked here, not a credential
that also drives MCP, the daemon and every `/api/v1/*` route.

Each row remembers which bearer signed it in (`client_token_id`), so revoking
that bearer, or deactivating the user, kills the session on the very next
request — `app/auth/ui_session.py::resolve_session` re-checks both every
time, not only at login. Expiry slides: a dashboard in daily use stays
signed in; one left alone for `SESSION_TTL` is gone.

Kernel-owned (imported in `app/models/__init__.py`) because the middleware
that reads it sits in `app/main.py`, ahead of every integration.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import UserOwnedMixin

#: Sliding lifetime of a dashboard session — the same 30 days the old
#: `ui_token` cookie carried, but counted from *last use*, not from login.
SESSION_TTL = timedelta(days=30)


class UiSession(UserOwnedMixin, Base):
    """Per-user (UserOwnedMixin supplies `user_id`, RESTRICT on delete — a
    user with live sessions is signed out explicitly, not by cascade)."""

    __tablename__ = "ui_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # sha256 of the 32 random bytes the cookie carries — same at-rest rule
    # as `client_tokens.token_hash` (`app/auth/hashing.py`): the DB never
    # holds a value that, read back, is a usable session.
    session_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
    )
    # The bearer that signed this session in. Nullable only so the FK can be
    # SET NULL if a token row is ever hard-deleted — `resolve_session`
    # treats NULL as "the bearer is gone" and refuses, the same as revoked.
    client_token_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("client_tokens.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
