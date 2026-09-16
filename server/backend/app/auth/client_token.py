"""Client bearer-token auth for the V3 HTTP API.

Validates an `Authorization: Bearer <token>` header against the
`client_tokens` table (hashed at rest — V4 chunk 2.3) and returns the
associated User row. Updates the token's `last_seen_at` on every
authenticated request, and slides `expires_at` back out by the same amount
— a token in daily use never expires; an unused one dies.

Returns the full User instance so handlers can use either `.id` (for
filtering per-user data) or `.name` (for vault folder paths, log lines,
display strings) without re-querying.

The resolved `ClientToken.id` is stashed on the returned (detached) `User`
instance as a dynamic `client_token_id` attribute — not a mapped column, just
a plain Python attribute set on this one object before it's handed back. That
lets a handler that needs to write onto *the exact token that authenticated
this request* (heartbeat — see F11a) do so without a second query or a
ContextVar, which would be unsafe here anyway: FastAPI runs each sync
dependency's callable via its own `run_in_threadpool` call, so a ContextVar
set inside `get_current_user`'s dependency call is not guaranteed visible to
the endpoint function's own call. Read it with `client_token_id_of()`. OAuth
sessions (`resolve_oauth_token_to_user`) never set it, so it's `None` there —
callers must treat that as "no row to write to", not an error.

The token's `scope` (2026-09-07 — `'full'` | `'readonly'`, see
`app.models.clients.TOKEN_SCOPES`) rides along the same way, as
`client_token_scope`, read with `client_token_scope_of()`. A resolver that
does not set it (OAuth sessions are a person's own connector) is `'full'`.
`get_current_user` enforces the HTTP half of `readonly` — see
`readonly_http_refusal()`; `app/plugin/dispatch.py` enforces the tool half.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import Header, HTTPException, Request, status

from app.auth.hashing import hash_token, token_last4
from app.auth.rate_limit import is_over_limit
from app.db import get_db
from app.models.clients import (
    DEFAULT_TOKEN_TTL,
    SCOPE_FULL,
    SCOPE_READONLY,
    ClientToken,
)
from app.models.users import User

logger = logging.getLogger(__name__)

# P3: every authenticated request used to write+commit a sliding
# last_seen_at/expires_at unconditionally — the hot path for every single
# MCP/API call. Heartbeats land every 5 minutes, so throttling the write to
# "at most once per this window" costs nothing in daemon-liveness granularity
# while cutting a write+commit off the vast majority of requests.
LAST_SEEN_WRITE_THROTTLE = timedelta(seconds=60)


def client_token_id_of(user: User) -> int | None:
    """Return the `ClientToken.id` that authenticated `user`'s request.

    `None` for OAuth-resolved sessions (no `client_tokens` row exists) or
    any other resolver that doesn't set it — callers must handle that as
    "nothing to write to", never raise.
    """
    return getattr(user, "client_token_id", None)


def client_token_scope_of(user: User) -> str:
    """Return the scope of the bearer that authenticated `user`'s request.

    `'full'` unless the resolver set `'readonly'`. OAuth-resolved sessions
    and any other resolver that doesn't set it are `'full'` — the absence of
    a scope is not a restriction, but note the inverse in `dispatch.py`: the
    absence of a `readOnlyHint` IS a write.
    """
    return getattr(user, "client_token_scope", None) or SCOPE_FULL


# The one non-GET a read-only bearer may make: a tool call, which
# `app/plugin/dispatch.py` then gates on the tool's own readOnlyHint.
_READONLY_POST_PREFIX = "/api/v1/tools/"


def readonly_http_refusal(user: User, method: str, path: str) -> str | None:
    """Why a read-only bearer may not make this request — or None if it may.

    The HTTP half of the `readonly` scope (the tool half is in
    `app/plugin/dispatch.py`). A read-only bearer may make any `GET` and may
    `POST /api/v1/tools/{name}`; every other method/path is a write by
    construction — vault push, inbox ingest, health push, reminders channel,
    log shipping — and is refused before the handler runs. `GET
    /api/v1/heartbeat` writes `client_version`/`task_health`, but only onto
    the token's own row, so it stays allowed with the other GETs.

    Returns a detail string (for a 403) so both the FastAPI dependency and
    `routes/inbox.py`'s hand-rolled bearer check say the same thing.
    """
    if client_token_scope_of(user) != SCOPE_READONLY:
        return None
    method = method.upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return None
    if (
        method == "POST"
        and path.startswith(_READONLY_POST_PREFIX)
        and len(path) > len(_READONLY_POST_PREFIX)
    ):
        return None
    return (
        f"read-only token: {method} {path} is a write. A read-only bearer may "
        f"only make GET requests and call read-only tools via POST /api/v1/tools/{{name}}."
    )


class TokenExpiredError(Exception):
    """Raised by `resolve_token_to_user` when the token is otherwise valid
    (known hash, active) but past `expires_at`.

    Callers catch this to log a distinct "expired" reason instead of the
    generic "invalid" one, then treat it as a 401 same as any other
    resolution failure. Kept as an exception (rather than returning a
    sentinel) so both call sites — `get_current_user` and
    `app/mcp/server.py::_authenticate_request` — log exactly once per
    failed request, with no risk of double-logging.
    """


def _client_ip(request: Request | None) -> str:
    if request is None or request.client is None:
        return "unknown"
    return request.client.host


def resolve_token_to_user(token: str) -> User | None:
    """Resolve a raw bearer string to a detached User row, or None if invalid.

    Raises `TokenExpiredError` if the token is known but expired — still a
    401 to the caller, just logged with a distinct reason.

    Used by both the FastAPI dependency (`get_current_user`) and the MCP
    transport in `app/mcp/server.py`, which can't use FastAPI's Depends.
    """
    if not token:
        return None

    now = datetime.now(timezone.utc)
    db = get_db()
    with db.session() as session:
        row = (
            session.query(ClientToken)
            .filter_by(token_hash=hash_token(token), is_active=True)
            .first()
        )
        if not row:
            return None
        if row.expires_at is not None and row.expires_at <= now:
            raise TokenExpiredError()

        # P3: only slide last_seen_at/expires_at (and commit) when the last
        # write is stale enough to matter. Every other authenticated request
        # in the throttle window reads the row and does nothing further.
        if row.last_seen_at is None or (now - row.last_seen_at) >= LAST_SEEN_WRITE_THROTTLE:
            row.last_seen_at = now
            # Sliding expiry — active daily use never expires.
            row.expires_at = now + DEFAULT_TOKEN_TTL

        token_id = row.id
        user = session.query(User).filter_by(id=row.user_id).first()
        # A deactivated user's tokens are refused even if the rows themselves
        # are still `is_active` — `routes/install.py` already gates redemption
        # on `User.is_active`; a live bearer must not outlive the account
        # (2026-09-06 scoping audit).
        if not user or not user.is_active:
            return None
        # Snapshot attributes BEFORE commit — sessionmaker uses
        # expire_on_commit=True (default), so accessing user.id after
        # commit/close would trigger a refresh against a closed session.
        uid, uname, udisplay = user.id, user.name, user.display_name
        # `is_admin` too: `/api/auth/login` answers from this object, and the
        # dashboard hid every admin control from Alex on 2026-09-06 because
        # the snapshot omitted it (the DB said true; the response said false).
        uadmin = bool(user.is_admin)
        # And the token's scope — same rule, same reason: anything read off a
        # row after commit is a refresh against a closed session, and a field
        # missing from this snapshot fails silently as its default.
        token_scope = row.scope or SCOPE_FULL
        session.commit()

    result = User(id=uid, name=uname, display_name=udisplay, is_active=True, is_admin=uadmin)
    # See module docstring: dynamic attribute, not a mapped column — lets
    # the heartbeat handler write onto the exact token that authenticated
    # this request instead of guessing at "most recently seen".
    result.client_token_id = token_id
    result.client_token_scope = token_scope
    return result


def get_current_user(
    authorization: str = Header(default=""), request: Request = None,
) -> User:
    """Validate the Bearer token; return the authenticated User row.

    Raises HTTPException(401) on missing, invalid, or expired tokens. Logs
    one structured warn line per failure (token last4 + source IP — never
    the full token or request body), with a distinct message for expired
    vs. invalid/unknown so ops can tell the difference.
    """
    from app.services.auth_events import record_auth_event

    client_ip = _client_ip(request)
    # F8: check the per-IP failure budget BEFORE any token parsing/DB lookup
    # or auth_events write — an over-limit caller is turned away for free.
    # Only 401s spend budget (recorded inside record_auth_event), so
    # legitimate traffic at any volume never trips this.
    if is_over_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many auth attempts — slow down",
        )

    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token:
        record_auth_event(outcome="401", source_ip=client_ip, transport="http")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user = resolve_token_to_user(token)
    except TokenExpiredError:
        logger.warning(
            "client_token auth failed (expired): last4=%s ip=%s",
            token_last4(token), client_ip,
        )
        record_auth_event(
            outcome="401", token_last4=token_last4(token),
            source_ip=client_ip, transport="http",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user is None:
        logger.warning(
            "client_token auth failed (invalid or unknown): last4=%s ip=%s",
            token_last4(token), client_ip,
        )
        record_auth_event(
            outcome="401", token_last4=token_last4(token),
            source_ip=client_ip, transport="http",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Scope (2026-09-07): a read-only bearer authenticates fine but may not
    # write. 403, not 401 — the caller IS who they say they are; the token
    # simply does not carry this authority. Checked here, in the one
    # dependency every bearer-authenticated route shares, so a new write
    # route is covered without remembering to add anything.
    if request is not None:
        refusal = readonly_http_refusal(user, request.method, request.url.path)
        if refusal is not None:
            logger.warning(
                "read-only token refused %s %s: user=%s token_id=%s",
                request.method, request.url.path, user.id, client_token_id_of(user),
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)
    return user
