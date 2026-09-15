"""Dashboard sessions + the admin gate (2026-09-06 — one credential).

Alex's decision: lios has ONE credential, the per-user bearer
(`client_tokens`). The dashboard used to be the exception — a shared
`HOME_UI_TOKEN` password in a cookie that authenticated a *browser* and named
no *person*, so nothing behind it could be scoped or attributed. It now signs
in a person:

    POST /api/auth/login {"token": <per-user bearer>}
        → resolve_token_to_user()            (active user, unexpired bearer)
        → create_session()                   (32 random bytes; sha256 stored)
        → Set-Cookie: lios_session=<random>  (httpOnly, secure, samesite=strict)

    every later /api/* request
        → resolve_session(cookie)            (session live, bearer still
                                              active+unexpired, user active)
        → the request runs inside use_user(user.id)

The cookie never carries the bearer: a leaked cookie is a revocable dashboard
session, not the credential that also drives MCP and the daemon.

**Binding the user for the request.** `app/auth/client_token.py`'s module
docstring explains why a ContextVar set inside a FastAPI *dependency* is
unsafe: each sync dependency runs in its own `run_in_threadpool` call, so a
value set there is not guaranteed visible to the endpoint. The session gate
therefore lives in a *middleware* (`app/main.py::check_ui_auth`) that wraps
`call_next` in `use_user(...)`. Downstream, Starlette copies the current
context into the task that runs the endpoint and — for `def` endpoints —
into the worker thread (`anyio.to_thread.run_sync` runs the function under
`contextvars.copy_context()`), so both async and sync routes see the binding.
`tests/test_ui_auth_middleware.py` pins both shapes.

Admin-only routes
-----------------
`is_admin` on `users` is the only role concept. `require_admin` is a FastAPI
dependency (403 for a signed-in non-admin, 401 if somehow unbound) applied
route by route. The rule used to classify every kernel router under
`app/routes/` and every manifest-mounted integration router, so the next
person can see it without re-deriving it:

    ADMIN ONLY — mints or revokes credentials, changes server-wide state, or
    reads/writes another person's data:
      POST   /api/auth/clients                 mint a bearer for anyone
      DELETE /api/auth/clients/{id}            revoke — unless it is the
                                               caller's own token (allowed)
      GET    /api/auth/tokens                  everyone's OAuth accounts —
                                               a non-admin sees only theirs
      DELETE /api/data/purge/{integration}     household-wide delete
      POST   /api/data/reindex-embeddings      server-wide rebuild
      PUT    /api/integrations/{name}/enabled
      POST   /api/integrations/{name}/sync
      GET    /api/integrations/{name}/config   secrets, even masked
      PUT    /api/integrations/{name}/config
      POST   /api/integrations/google_mail/backfill | /embed
      POST   /api/integrations/lastfm/backfill
      POST   /api/integrations/historical_corpus/ingest
      GET/PUT /api/preferences/{user_id}       for a user_id other than the
                                               caller's own
      GET    /api/logs/, /tail, /users         other users' logs — a non-admin
                                               is filtered to their own
      GET    /api/strava/connect?user=         starts a grant against a named
                                               user — self only unless admin

    ANY SIGNED-IN USER — the household admin view (read-only, or reads a
    person's own data):
      GET    /api/dashboard/summary            (cross-user by design — see
                                               server/CLAUDE.md "Per-user
                                               request scoping")
      GET    /api/system/*                     info / alerts / tool-stats /
                                               background-tasks
      GET    /api/integrations/                list, and /{name}/detail,
                                               /{name}/tools
      GET    /api/auth/clients                 own tokens (admin: all)
      GET    /api/auth/check, POST /api/auth/logout
      GET    /api/data/stats
      GET    /api/preferences/schema, and /{own user_id}

    OWN AUTH, NOT SESSION-GATED (unchanged — `AUTH_EXEMPT*` in main.py):
      /api/v1/*, /api/inbox/*, /api/health/*, /api/reminders/*,
      /api/client/*, /api/install/*   — per-user bearer or install code
      /api/auth/login, /api/auth/google/*, /api/strava/callback, /api/health
                                      — the login itself, and OAuth return
                                        legs (HMAC state is their auth)

`GET /api/auth/google/login?user=` stays exempt for the cross-domain reason
documented in main.py, but since 2026-09-07 it is no longer open: it demands a
signed ten-minute `start` (account + user + expiry, HKDF-derived key with its
own label — `app/auth/oauth.py::sign_login_start`) that only an authenticated
context mints. The session-gated way to get one is
`GET /api/auth/google/login-url?account=&user=` (self only unless admin — the
same rule as `strava/connect`); the dashboard summary and `system_alerts`
embed one in each `reauth_url`. The asymmetry with `strava/connect` is now
cosmetic: both require the caller to have authenticated first, one via cookie
and one via a proof carried in the URL.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status

from app.auth.hashing import hash_token
from app.db import get_db
from app.models.clients import SCOPE_READONLY, ClientToken
from app.models.ui_sessions import SESSION_TTL, UiSession
from app.models.users import User

logger = logging.getLogger(__name__)

SESSION_COOKIE = "lios_session"

# Same shape as `client_token.LAST_SEEN_WRITE_THROTTLE`: the dashboard polls
# several endpoints a minute, and every one of them would otherwise be a
# write+commit on the hot path.
LAST_SEEN_WRITE_THROTTLE = timedelta(seconds=60)


def _snapshot(user: User, *, client_token_id: int | None) -> User:
    """A detached `User` carrying the same dynamic `client_token_id` the
    bearer resolver sets (see `client_token.client_token_id_of`)."""
    result = User(
        id=user.id, name=user.name, display_name=user.display_name,
        is_active=user.is_active, is_admin=user.is_admin,
    )
    result.client_token_id = client_token_id
    return result


def create_session(user: User) -> str:
    """Open a session for an already-authenticated user; return the cookie value.

    `user` must have come from `resolve_token_to_user` — its
    `client_token_id` is what ties the session to the bearer that opened it,
    so a revoked bearer takes its sessions with it. Refuses (rather than
    opening an unrevocable session) if that attribute is missing.
    """
    token_id = getattr(user, "client_token_id", None)
    if token_id is None:
        raise ValueError("a dashboard session must be opened by a client_tokens bearer")
    plaintext = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db = get_db()
    with db.session() as session:
        session.add(
            UiSession(
                session_hash=hash_token(plaintext),
                user_id=user.id,
                client_token_id=token_id,
                created_at=now,
                expires_at=now + SESSION_TTL,
                last_seen_at=now,
            )
        )
        session.commit()
    return plaintext


def resolve_session(cookie_value: str | None) -> User | None:
    """The user a session cookie belongs to, or None if it must be refused.

    Checked on EVERY request, not just at login: the session row exists, is
    unexpired, the bearer that opened it is still active and unexpired, and
    the user is still active. Any one failing → None. Slides `expires_at`
    (throttled) so daily use never expires.
    """
    if not cookie_value:
        return None
    now = datetime.now(timezone.utc)
    db = get_db()
    with db.session() as session:
        row = (
            session.query(UiSession)
            .filter_by(session_hash=hash_token(cookie_value))
            .first()
        )
        if row is None or row.expires_at <= now:
            return None
        if row.client_token_id is None:
            return None
        token = session.query(ClientToken).filter_by(id=row.client_token_id).first()
        if token is None or not token.is_active:
            return None
        if token.expires_at is not None and token.expires_at <= now:
            return None
        # Defence in depth for the login-time refusal in `routes/auth.py`: a
        # read-only bearer never opens a session, and a session whose bearer
        # has somehow become read-only since is closed on the next request.
        if token.scope == SCOPE_READONLY:
            return None
        user = session.query(User).filter_by(id=row.user_id).first()
        if user is None or not user.is_active:
            return None
        if row.last_seen_at is None or (now - row.last_seen_at) >= LAST_SEEN_WRITE_THROTTLE:
            row.last_seen_at = now
            row.expires_at = now + SESSION_TTL
        result = _snapshot(user, client_token_id=token.id)
        session.commit()
    return result


def delete_session(cookie_value: str | None) -> bool:
    """Sign out: remove the row. True if something was deleted."""
    if not cookie_value:
        return False
    db = get_db()
    with db.session() as session:
        deleted = (
            session.query(UiSession)
            .filter_by(session_hash=hash_token(cookie_value))
            .delete(synchronize_session=False)
        )
        session.commit()
    return bool(deleted)


def current_ui_user(request: Request) -> User:
    """The signed-in user the middleware attached to this request.

    FastAPI dependency. 401 if the middleware did not bind one — which for a
    session-gated route means the gate was bypassed, so refusing is right.
    """
    user = getattr(request.state, "ui_user", None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in",
        )
    return user


def require_admin(request: Request) -> User:
    """FastAPI dependency: the signed-in user, who must be an admin (else 403).

    See the module docstring for which routes carry it and why.
    """
    user = current_ui_user(request)
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin only",
        )
    return user
