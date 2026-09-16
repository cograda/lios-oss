"""OAuth 2.1 Authorization Server provider for Comar's MCP.

Structurally implements `mcp.server.auth.provider.OAuthAuthorizationServerProvider`
so the SDK can host /authorize, /token, /register, /revoke and the metadata docs —
we supply only persistence + the human-login funnel. See vault Plans/mcp-oauth.md.

PKCE (S256) is verified inside the SDK's token handler against the `code_challenge`
we persist and return from `load_authorization_code`; we don't implement it here.

The human-login step lives in `oauth_wire.py`: since 2026-09-06 it takes the
person's own per-user bearer and completes the login as that user (the
earlier Phase-0 form minted a session as user_id=1 from a shared UI token).
Everything below (token minting, code exchange, refresh rotation,
revocation) is unchanged by that swap.
"""

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.auth.hashing import hash_token
from app.db import get_db
from app.models.oauth_clients import (
    McpAccessToken,
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthLoginSession,
)
from app.models.users import User

logger = logging.getLogger(__name__)

ACCESS_TTL = timedelta(seconds=3600)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(seconds=60)
LOGIN_SESSION_TTL = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Subclasses carry our `user_id` through the SDK untouched — provider.py:99
# explicitly sanctions adding non-exposed fields to these models.
class ComarAuthorizationCode(AuthorizationCode):
    user_id: int


class ComarAccessToken(AccessToken):
    user_id: int


class ComarRefreshToken(RefreshToken):
    user_id: int


class TokenExpiredError(Exception):
    """Raised by `resolve_oauth_token_to_user` so callers can log a distinct
    401 reason ("expired" vs "invalid/unknown").

    See `app/mcp/server.py::_authenticate_request`, which catches this,
    logs it, and then treats it the same as any other auth failure (401,
    no user).
    """


def resolve_oauth_token_to_user(token: str) -> User | None:
    """Sync resolver for the MCP bearer path (`_authenticate_request`).

    Mirrors `app.auth.client_token.resolve_token_to_user`: returns a detached
    `User` for a valid, unrevoked OAuth access token, else None. Raises
    `TokenExpiredError` (still resulting in a 401) if the token is otherwise
    valid but past `expires_at`, so the caller can log a distinct reason.
    """
    if not token:
        return None
    now = _utcnow()
    db = get_db()
    with db.session() as session:
        row = (
            session.query(McpAccessToken)
            .filter_by(access_token_hash=hash_token(token), revoked=False)
            .first()
        )
        if not row:
            return None
        if row.expires_at <= now:
            raise TokenExpiredError()
        user = session.query(User).filter_by(id=row.user_id).first()
        if not user:
            return None
        # Snapshot before the session closes (expire_on_commit default).
        uid, uname, udisplay = user.id, user.name, user.display_name
    return User(id=uid, name=uname, display_name=udisplay)


class ComarOAuthProvider:
    """Persistence + identity for the SDK-hosted OAuth endpoints.

    Not declared as a subclass of the `OAuthAuthorizationServerProvider` Protocol
    (it's a Generic Protocol; the SDK uses it structurally), but implements every
    method it requires.
    """

    # ---- clients (DCR) -------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        db = get_db()
        with db.session() as session:
            row = session.query(OAuthClient).filter_by(client_id=client_id).first()
            if not row:
                return None
            return OAuthClientInformationFull.model_validate_json(row.data)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        db = get_db()
        with db.session() as session:
            data = client_info.model_dump_json()
            existing = (
                session.query(OAuthClient)
                .filter_by(client_id=client_info.client_id)
                .first()
            )
            if existing:
                existing.data = data
            else:
                session.add(OAuthClient(client_id=client_info.client_id, data=data))
            session.commit()

    # ---- authorize → park for human login ------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the human to our login funnel.

        Returns a redirect URL (the SDK's AuthorizationHandler 302s the browser
        there) at /oauth/login — see `oauth_wire.py` for what that route
        currently does.
        """
        session_id = secrets.token_urlsafe(32)
        db = get_db()
        with db.session() as session:
            session.add(OAuthLoginSession(
                session_id=session_id,
                client_id=client.client_id,
                params=params.model_dump_json(),
                expires_at=_utcnow() + LOGIN_SESSION_TTL,
            ))
            session.commit()
        return f"/oauth/login?login_session={session_id}"

    def complete_login(self, session_id: str, user_id: int) -> str | None:
        """Consume a login session, mint an auth code bound to `user_id`, and
        return the client redirect URL (code + state). None if expired/unknown.

        Sync — called from the /oauth/login POST handler after the human is
        authenticated.
        """
        now = _utcnow()
        db = get_db()
        with db.session() as session:
            ls = (
                session.query(OAuthLoginSession)
                .filter_by(session_id=session_id)
                .first()
            )
            if not ls or ls.expires_at <= now:
                return None
            params = json.loads(ls.params)
            code = secrets.token_urlsafe(32)
            session.add(OAuthAuthorizationCode(
                code=code,
                client_id=ls.client_id,
                user_id=user_id,
                code_challenge=params["code_challenge"],
                redirect_uri=str(params["redirect_uri"]),
                redirect_uri_provided_explicitly=params.get(
                    "redirect_uri_provided_explicitly", True
                ),
                scopes=json.dumps(params.get("scopes") or []),
                resource=params.get("resource"),
                expires_at=now + CODE_TTL,
            ))
            session.delete(ls)
            redirect_uri = str(params["redirect_uri"])
            state = params.get("state")
            session.commit()
        return construct_redirect_uri(redirect_uri, code=code, state=state)

    # ---- authorization codes ------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> ComarAuthorizationCode | None:
        now = _utcnow()
        db = get_db()
        with db.session() as session:
            row = (
                session.query(OAuthAuthorizationCode)
                .filter_by(code=authorization_code, client_id=client.client_id, consumed=False)
                .first()
            )
            if not row or row.expires_at <= now:
                return None
            return ComarAuthorizationCode(
                code=row.code,
                scopes=json.loads(row.scopes),
                expires_at=row.expires_at.timestamp(),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
                user_id=row.user_id,
            )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: ComarAuthorizationCode,
    ) -> OAuthToken:
        now = _utcnow()
        db = get_db()
        with db.session() as session:
            row = (
                session.query(OAuthAuthorizationCode)
                .filter_by(code=authorization_code.code, client_id=client.client_id, consumed=False)
                .first()
            )
            if not row or row.expires_at <= now:
                raise TokenError("invalid_grant", "Authorization code invalid or expired")
            row.consumed = True
            scopes = row.scopes
            new_row, access, refresh = McpAccessToken.mint(
                client_id=row.client_id,
                user_id=row.user_id,
                scopes=scopes,
                resource=row.resource,
                expires_at=now + ACCESS_TTL,
                refresh_expires_at=now + REFRESH_TTL,
            )
            session.add(new_row)
            session.commit()
        scope_str = " ".join(json.loads(scopes))
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=int(ACCESS_TTL.total_seconds()),
            refresh_token=refresh,
            scope=scope_str or None,
        )

    # ---- refresh tokens -----------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> ComarRefreshToken | None:
        now = _utcnow()
        db = get_db()
        with db.session() as session:
            row = (
                session.query(McpAccessToken)
                .filter_by(
                    refresh_token_hash=hash_token(refresh_token),
                    client_id=client.client_id, revoked=False,
                )
                .first()
            )
            if not row:
                return None
            if row.refresh_expires_at and row.refresh_expires_at <= now:
                return None
            return ComarRefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=json.loads(row.scopes),
                expires_at=int(row.refresh_expires_at.timestamp()) if row.refresh_expires_at else None,
                user_id=row.user_id,
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: ComarRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        now = _utcnow()
        db = get_db()
        with db.session() as session:
            row = (
                session.query(McpAccessToken)
                .filter_by(
                    refresh_token_hash=hash_token(refresh_token.token),
                    client_id=client.client_id, revoked=False,
                )
                .first()
            )
            if not row or (row.refresh_expires_at and row.refresh_expires_at <= now):
                raise TokenError("invalid_grant", "Refresh token invalid or expired")
            # Rotate both tokens (SDK guidance): revoke the old row, mint a new one.
            row.revoked = True
            new_scopes = json.dumps(scopes) if scopes else row.scopes
            new_row, access, refresh = McpAccessToken.mint(
                client_id=row.client_id,
                user_id=row.user_id,
                scopes=new_scopes,
                resource=row.resource,
                expires_at=now + ACCESS_TTL,
                refresh_expires_at=now + REFRESH_TTL,
            )
            session.add(new_row)
            session.commit()
        scope_str = " ".join(json.loads(new_scopes))
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=int(ACCESS_TTL.total_seconds()),
            refresh_token=refresh,
            scope=scope_str or None,
        )

    # ---- access tokens + revocation -----------------------------------

    async def load_access_token(self, token: str) -> ComarAccessToken | None:
        now = _utcnow()
        db = get_db()
        with db.session() as session:
            row = (
                session.query(McpAccessToken)
                .filter_by(access_token_hash=hash_token(token), revoked=False)
                .first()
            )
            if not row or row.expires_at <= now:
                return None
            return ComarAccessToken(
                token=token,
                client_id=row.client_id,
                scopes=json.loads(row.scopes),
                expires_at=int(row.expires_at.timestamp()),
                resource=row.resource,
                user_id=row.user_id,
            )

    async def revoke_token(
        self, token: ComarAccessToken | ComarRefreshToken
    ) -> None:
        # Revoke the whole row regardless of which token (access/refresh) we hold.
        value_hash = hash_token(token.token)
        db = get_db()
        with db.session() as session:
            row = (
                session.query(McpAccessToken)
                .filter(
                    (McpAccessToken.access_token_hash == value_hash)
                    | (McpAccessToken.refresh_token_hash == value_hash)
                )
                .first()
            )
            if row:
                row.revoked = True
                session.commit()


# Module-level singleton — imported by oauth_wire.py (route wiring) and by
# app/mcp/server.py (via resolve_oauth_token_to_user, the sync seam).
provider = ComarOAuthProvider()
