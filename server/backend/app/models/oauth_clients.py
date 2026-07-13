"""OAuth 2.1 authorization-server persistence (MCP connector sign-in).

Backs the provider in `app/auth/oauth_provider.py` so claude.ai's custom-connector
flow (discovery → DCR → auth-code → PKCE) can authenticate against Comar's MCP.
Four tables:

  oauth_clients              — DCR-registered clients (claude.ai self-registers)
  oauth_login_sessions       — transient: a parked /authorize request, held while
                               the human signs in (the federation funnel)
  oauth_authorization_codes  — single-use codes, bound to a user_id
  mcp_access_tokens          — issued access + refresh tokens (the bearer the MCP
                               layer validates)

Access/refresh tokens are high-entropy random secrets stored in plaintext — the
same posture as `client_tokens`: equality-lookup by value requires it, and the
Tailscale-only exposure + short TTLs bound the risk. Encryption-at-rest is a
later hardening (Plans/mcp-oauth.md, Phase 2).
"""

import secrets
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import UserOwnedMixin


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OAuthClient(Base):
    """A dynamically-registered OAuth client (RFC 7591).

    Global, not per-user — a client (e.g. claude.ai) is shared infrastructure;
    it's the *authorization* step that binds each issued token to a user.
    The full `OAuthClientInformationFull` is stored verbatim as JSON.
    """

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )


class OAuthLoginSession(Base):
    """A parked /authorize request awaiting human sign-in (the funnel).

    The SDK's AuthorizationHandler validates the client + PKCE challenge, then
    calls `provider.authorize()`, which stashes the `AuthorizationParams` here
    and redirects the human to `/oauth/login`. On success we mint an auth code
    bound to the user and redirect back to the client's `redirect_uri`.
    """

    __tablename__ = "oauth_login_sessions"

    session_id: Mapped[str] = mapped_column(
        String(64), primary_key=True,
        default=lambda: secrets.token_urlsafe(32),
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # AuthorizationParams as JSON (redirect_uri, code_challenge, state, scopes, resource).
    params: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class OAuthAuthorizationCode(UserOwnedMixin, Base):
    """Single-use authorization code (~60s TTL), bound to the user who signed in."""

    __tablename__ = "oauth_authorization_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True,
        default=lambda: secrets.token_urlsafe(32),
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON list
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )


class McpAccessToken(UserOwnedMixin, Base):
    """An issued access token (+ refresh token) for an MCP connector.

    `access_token` is the bearer the MCP layer validates — see
    `app/mcp/server.py::_authenticate_request` →
    `app.auth.oauth_provider.resolve_oauth_token_to_user`.
    """

    __tablename__ = "mcp_access_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_token: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True,
        default=lambda: secrets.token_urlsafe(32),
    )
    refresh_token: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True, index=True,
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON list
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
