"""OAuth flow routes for Google services + dashboard sign-in + client tokens.

Dashboard sign-in (2026-09-06 — one credential: the per-user bearer): a
person posts their own `client_tokens` bearer once; the server opens a
`ui_sessions` row and sets a cookie holding a random session id. The bearer
is never stored in the cookie. See `app/auth/ui_session.py`.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth.client_token import TokenExpiredError, resolve_token_to_user
from app.auth.hashing import token_last4
from app.auth.oauth import (
    create_auth_url,
    exchange_code,
    google_login_url,
    verify_login_start,
)
from app.auth.rate_limit import is_over_limit, record_failure
from app.auth.ui_session import (
    SESSION_COOKIE,
    create_session,
    current_ui_user,
    delete_session,
    require_admin,
)
from app.config import settings
from app.db import get_db
from app.models.clients import SCOPE_FULL, TOKEN_SCOPES, ClientToken
from app.models.tokens import OAuthToken
from app.models.users import User

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "display_name": user.display_name,
        "is_admin": bool(user.is_admin),
    }


@router.post("/login")
async def login(request: Request):
    """Sign in with a per-user bearer; set a session cookie.

    Body: `{"token": "<client_tokens bearer>"}` — the same field name the
    frontend has always posted. Resolved with `resolve_token_to_user` (active
    user, unexpired bearer). The cookie holds a random session id, never the
    bearer.

    F8: the sign-in endpoint is a guessable-secret boundary — same per-IP
    failure budget as the bearer paths, checked before anything else; every
    401 here spends budget via `record_failure`.
    """
    client_ip = request.client.host if request.client else "unknown"
    if is_over_limit(client_ip):
        return JSONResponse({"error": "Too many attempts"}, status_code=429)

    body = await request.json()
    token = (body.get("token") or "").strip() if isinstance(body, dict) else ""
    if not token:
        record_failure(client_ip)
        return JSONResponse({"error": "token is required"}, status_code=401)
    try:
        user = resolve_token_to_user(token)
    except TokenExpiredError:
        user = None
    if user is None:
        record_failure(client_ip)
        from app.services.auth_events import record_auth_event

        record_auth_event(
            outcome="401", token_last4=token_last4(token),
            source_ip=client_ip, transport="ui",
        )
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    # A read-only bearer (2026-09-07) is a device credential — the Hall
    # Panel's — not a person's sign-in. The dashboard's write routes are
    # gated by session, not by the bearer dependency that enforces scope, so
    # rather than teach every one of them about scope, a read-only bearer
    # simply cannot open a session at all. Fail closed, name the reason.
    from app.auth.client_token import client_token_scope_of
    from app.models.clients import SCOPE_READONLY

    if client_token_scope_of(user) == SCOPE_READONLY:
        return JSONResponse(
            {"error": "A read-only token cannot open a dashboard session"},
            status_code=403,
        )

    session_id = create_session(user)
    response = JSONResponse({"status": "ok", "user": _user_payload(user)})
    response.set_cookie(
        SESSION_COOKIE, session_id,
        httponly=True, secure=True, samesite="strict", max_age=86400 * 30,
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    """Delete the session row and clear the cookie."""
    delete_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/check")
async def check_auth(request: Request):
    """Who is signed in, if anyone. Exempt from the gate so the SPA can ask."""
    from app.auth.ui_session import resolve_session

    user = resolve_session(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return {"authenticated": False, "user": None}
    return {"authenticated": True, "user": _user_payload(user)}


def _redirect_uri(request: Request) -> str:
    """Build the OAuth redirect URI from the current request.

    If oauth_redirect_base is set, use it instead of the request host.
    This is needed because Google rejects private IPs as redirect URIs.
    """
    if settings.oauth_redirect_base:
        return f"{settings.oauth_redirect_base.rstrip('/')}/api/auth/google/callback"
    return str(request.url_for("google_callback"))


@router.get("/google/login")
async def google_login(request: Request, account: str, user: str, start: str = ""):
    """Start Google OAuth flow for a given account email.

    `?user=` is REQUIRED and attributes the resulting token to that User row —
    there is no default. A forgotten `?user=` used to silently default to
    "alex", which would attribute a re-auth done during someone else's setup
    (e.g. Sam's) to Alex's account instead.

    `?start=` is REQUIRED too (2026-09-07). This route is exempt from the
    dashboard session because the re-auth link is followed on the Tailscale
    hostname where the `comar.lab` cookie is not sent (see `AUTH_EXEMPT` in
    app/main.py) — which used to mean anyone on the tailnet could start a
    Google grant naming any user. `start` is a ten-minute HMAC over
    (account, user, expiry) that only an already-authenticated context can
    mint: `GET /api/auth/google/login-url` (session), the dashboard summary's
    `reauth_needed[].reauth_url` (session), or `system_alerts` (bearer). A
    missing, tampered, mismatched or expired `start` is a 403 — the URL is
    not hand-typeable any more, by design; get one from the dashboard.
    """
    if not settings.google_client_id:
        return {"error": "Google OAuth not configured — set HOME_GOOGLE_CLIENT_ID and HOME_GOOGLE_CLIENT_SECRET"}

    try:
        verify_login_start(start, account_email=account, user_name=user)
    except ValueError as exc:
        return JSONResponse(
            {"error": f"login start rejected: {exc} — open the reconnect link from the dashboard"},
            status_code=403,
        )

    # Validate user exists before launching browser dance.
    from app.models.users import User
    db = get_db()
    with db.session() as session:
        if not session.query(User).filter_by(name=user).first():
            return JSONResponse(
                {"error": f"unknown user '{user}' — must exist in users table"},
                status_code=400,
            )

    auth_url = create_auth_url(account, _redirect_uri(request), user_name=user)
    return RedirectResponse(auth_url)


@router.get("/google/login-url")
async def google_login_link(account: str, user: str, caller: User = Depends(current_ui_user)):
    """Mint a `google/login` URL carrying a signed `start` for the signed-in
    person. The SPA's Connect/Reconnect buttons call this, then navigate to
    the returned URL — the URL itself is what the exempt route trusts.

    Self only unless admin, the same rule as `strava/connect`: starting a
    grant against someone else's row is an admin act.
    """
    if not caller.is_admin and caller.name != user:
        return JSONResponse(
            {"error": "you may only connect a Google account for your own user"},
            status_code=403,
        )
    return {"url": google_login_url(account, user)}


@router.get("/google/callback")
async def google_callback(request: Request, code: str, state: str):
    """Handle Google OAuth callback — exchange code for tokens."""
    db = get_db()
    with db.session() as session:
        token = exchange_code(code, state, _redirect_uri(request), session)
        return {
            "status": "ok",
            "account": token.account_email,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "message": f"Token stored for {token.account_email}. You can close this tab.",
        }


@router.get("/tokens")
async def list_tokens(caller: User = Depends(current_ui_user)):
    """List stored OAuth tokens with expiry status (no secrets exposed).

    An admin sees every account; anyone else sees only their own — the
    household's connected Google accounts are per-person data.

    Includes `user` (the owning User's name) — `google/login` now requires
    `?user=`, so any UI building a reconnect/reauth link from this list needs
    the name to build a correct URL, not just the account email.
    """
    db = get_db()
    with db.session() as session:
        q = session.query(OAuthToken, User).join(User, OAuthToken.user_id == User.id)
        if not caller.is_admin:
            q = q.filter(OAuthToken.user_id == caller.id)
        rows = q.all()
        now = datetime.now(timezone.utc)
        return {
            "tokens": [
                {
                    "provider": t.provider,
                    "account": t.account_email,
                    "user": u.name,
                    "scopes": t.scopes,
                    "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                    "expired": t.expires_at < now if t.expires_at else False,
                    "has_refresh_token": t.refresh_token is not None,
                }
                for t, u in rows
            ]
        }


# ---------------------------------------------------------------------------
# Client token management (gRPC auth tokens for comar-client daemons)
# ---------------------------------------------------------------------------


@router.get("/clients")
async def list_clients(caller: User = Depends(current_ui_user)):
    """List client tokens (never exposes the full token value).

    An admin sees every token; anyone else sees their own — cheap to allow,
    and it lets a person revoke a lost phone without asking the admin.
    """
    import json as _json

    db = get_db()
    with db.session() as session:
        q = session.query(ClientToken, User).join(User, ClientToken.user_id == User.id)
        if not caller.is_admin:
            q = q.filter(ClientToken.user_id == caller.id)
        rows = q.order_by(ClientToken.created_at.desc()).all()

        def _task_health(raw: str | None):
            # F11c: surfaced to the dashboard as parsed JSON, not the raw
            # string — malformed/legacy content shouldn't break the whole
            # clients list, so degrade to null rather than raise.
            if not raw:
                return None
            try:
                return _json.loads(raw)
            except (TypeError, ValueError):
                return None

        return {
            "clients": [
                {
                    "id": c.id,
                    "user": u.name,
                    "user_id": c.user_id,
                    "label": c.label,
                    "scope": c.scope,
                    "is_active": c.is_active,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                    "client_version": c.client_version,
                    "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                    "token_preview": f"...{c.token_last4}",
                    "task_health": _task_health(c.task_health),
                }
                for c, u in rows
            ]
        }


@router.post("/clients")
async def create_client(request: Request, _admin: User = Depends(require_admin)):
    """Create a new client token. Returns the full token value ONCE. Admin only
    — minting a bearer for anyone is the one thing that must not be open to
    every signed-in person."""
    body = await request.json()
    user_name = (body.get("user") or "").strip()
    label = (body.get("label") or "").strip()
    # Optional `scope` (2026-09-07): `full` (default — the user's whole
    # authority) or `readonly` (a device that may look but never write; see
    # `app.models.clients.TOKEN_SCOPES`). Validated here — the only write path
    # for the column — so a typo cannot mint a token of unknown scope.
    scope = (body.get("scope") or SCOPE_FULL).strip().lower()

    if not user_name or len(user_name) > 50:
        return JSONResponse({"error": "user is required (max 50 chars)"}, status_code=400)
    if not label or len(label) > 100:
        return JSONResponse({"error": "label is required (max 100 chars)"}, status_code=400)
    if scope not in TOKEN_SCOPES:
        return JSONResponse(
            {"error": f"scope must be one of: {', '.join(TOKEN_SCOPES)}"}, status_code=400,
        )

    db = get_db()
    with db.session() as session:
        user_row = session.query(User).filter_by(name=user_name).first()
        if not user_row:
            return JSONResponse(
                {"error": f"unknown user '{user_name}' — must exist in users table"},
                status_code=400,
            )
        client, plaintext = ClientToken.mint(user_id=user_row.id, label=label, scope=scope)
        session.add(client)
        session.commit()
        session.refresh(client)

        from app.services.auth_events import record_auth_event

        record_auth_event(
            outcome="issued",
            token_last4=client.token_last4,
            source_ip=request.client.host if request.client else None,
            transport="http",
            user_id=client.user_id,
        )
        return JSONResponse(
            {
                "id": client.id,
                "user": user_row.name,
                "user_id": client.user_id,
                "label": client.label,
                "scope": client.scope,
                "token": plaintext,
                "expires_at": client.expires_at.isoformat() if client.expires_at else None,
                "message": "Save this token now — it will not be shown again.",
            },
            status_code=201,
        )


@router.delete("/clients/{client_id}")
async def deactivate_client(
    client_id: int, request: Request, caller: User = Depends(current_ui_user),
):
    """Deactivate a client token (soft-delete — row preserved for audit).

    Admin, or the token's own user: revoking your own lost device is
    allowed; revoking anyone else's is not (403, and a 404 is not leaked for
    a token that exists but is not yours — the same 403).
    """
    db = get_db()
    with db.session() as session:
        client = session.query(ClientToken).filter_by(id=client_id).first()
        if not client:
            return JSONResponse({"error": "Client token not found"}, status_code=404)
        if not caller.is_admin and client.user_id != caller.id:
            return JSONResponse({"error": "Admin only"}, status_code=403)
        client.is_active = False
        token_last4_val, user_id = client.token_last4, client.user_id
        session.commit()

        from app.services.auth_events import record_auth_event

        record_auth_event(
            outcome="revoked",
            token_last4=token_last4_val,
            source_ip=request.client.host if request.client else None,
            transport="http",
            user_id=user_id,
        )
        return {"status": "ok", "id": client_id}
