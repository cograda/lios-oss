"""OAuth flow routes for Google services + UI auth + client token management."""

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth.oauth import create_auth_url, exchange_code
from app.auth.utils import safe_token_check
from app.config import settings
from app.db import get_db
from app.models.clients import ClientToken
from app.models.tokens import OAuthToken

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(request: Request):
    """Validate UI token and set a cookie."""
    body = await request.json()
    token = body.get("token", "")
    if not settings.ui_token:
        return {"status": "ok", "message": "Auth not configured"}
    if not safe_token_check(token, settings.ui_token):
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    response = JSONResponse({"status": "ok"})
    # secure=True: this app has no separate "behind TLS" setting, and the
    # documented dashboard access points (https://comar.lab via Caddy,
    # https://<tailnet>.ts.net via Tailscale) both terminate TLS in front of
    # this process. A browser will simply not attach this cookie on a plain
    # http:// origin — use the Tailscale/Caddy hostname, not the raw
    # http://SERVER_IP:8400 LAN address, when logging into the dashboard.
    response.set_cookie(
        "ui_token", token, httponly=True, samesite="strict", secure=True, max_age=86400 * 30,
    )
    return response


@router.get("/check")
async def check_auth(request: Request):
    """Check if the current session is authenticated."""
    if not settings.ui_token:
        return {"authenticated": True, "auth_required": False}
    token = request.cookies.get("ui_token")
    return {"authenticated": safe_token_check(token, settings.ui_token), "auth_required": True}


def _redirect_uri(request: Request) -> str:
    """Build the OAuth redirect URI from the current request.

    If oauth_redirect_base is set, use it instead of the request host.
    This is needed because Google rejects private IPs as redirect URIs.
    """
    if settings.oauth_redirect_base:
        return f"{settings.oauth_redirect_base.rstrip('/')}/api/auth/google/callback"
    return str(request.url_for("google_callback"))


@router.get("/google/login")
async def google_login(request: Request, account: str, user: str = "alex"):
    """Start Google OAuth flow for a given account email.

    Visit this URL in a browser to grant access. The optional `?user=` param
    attributes the resulting token to that User row (defaults to alex for
    back-compat). For Sam's account: `?account=sam@example.com&user=sam`.

    Example: /api/auth/google/login?account=alex@example.com&user=alex
    """
    if not settings.google_client_id:
        return {"error": "Google OAuth not configured — set HOME_GOOGLE_CLIENT_ID and HOME_GOOGLE_CLIENT_SECRET"}

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
async def list_tokens():
    """List all stored tokens with expiry status (no secrets exposed)."""
    db = get_db()
    with db.session() as session:
        tokens = session.query(OAuthToken).all()
        now = datetime.now(timezone.utc)
        return {
            "tokens": [
                {
                    "provider": t.provider,
                    "account": t.account_email,
                    "scopes": t.scopes,
                    "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                    "expired": t.expires_at < now if t.expires_at else False,
                    "has_refresh_token": t.refresh_token is not None,
                }
                for t in tokens
            ]
        }


# ---------------------------------------------------------------------------
# Client token management (gRPC auth tokens for comar-client daemons)
# ---------------------------------------------------------------------------


@router.get("/clients")
async def list_clients():
    """List client tokens for every household member (never exposes the full token value).

    Gated only by the shared HOME_UI_TOKEN, same as the rest of the
    dashboard (e.g. /api/dashboard/summary) — this app has no per-user
    browser session concept, so the UI token already implies full
    household-admin visibility everywhere else. Scoping just this route to
    a per-user bearer would be inconsistent with that model and the
    dashboard has no bearer to send anyway.
    """
    from app.models.users import User as UserModel

    db = get_db()
    with db.session() as session:
        rows = (
            session.query(ClientToken, UserModel)
            .join(UserModel, ClientToken.user_id == UserModel.id)
            .order_by(ClientToken.created_at.desc())
            .all()
        )
        return {
            "clients": [
                {
                    "id": c.id,
                    "user": u.name,
                    "user_id": c.user_id,
                    "label": c.label,
                    "is_active": c.is_active,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                    "client_version": c.client_version,
                    "token_preview": f"{c.token[:4]}...{c.token[-4:]}",
                }
                for c, u in rows
            ]
        }


@router.post("/clients")
async def create_client(request: Request):
    """Create a new client token for any household member. Returns the full token value ONCE.

    Gated only by the shared HOME_UI_TOKEN — see list_clients for why this
    route doesn't add a separate per-user bearer requirement. The frontend
    prompts for confirmation before minting a token for a user other than
    the one currently viewing Settings, as light friction against mistakes.
    """
    from app.models.users import User as UserModel

    body = await request.json()
    user_name = (body.get("user") or "").strip()
    label = (body.get("label") or "").strip()

    if not user_name or len(user_name) > 50:
        return JSONResponse({"error": "user is required (max 50 chars)"}, status_code=400)
    if not label or len(label) > 100:
        return JSONResponse({"error": "label is required (max 100 chars)"}, status_code=400)

    db = get_db()
    with db.session() as session:
        user_row = session.query(UserModel).filter_by(name=user_name).first()
        if not user_row:
            return JSONResponse(
                {"error": f"unknown user '{user_name}' — must exist in users table"},
                status_code=400,
            )
        client = ClientToken(user_id=user_row.id, label=label)
        session.add(client)
        session.commit()
        session.refresh(client)
        return JSONResponse(
            {
                "id": client.id,
                "user": user_row.name,
                "user_id": client.user_id,
                "label": client.label,
                "token": client.token,
                "message": "Save this token now — it will not be shown again.",
            },
            status_code=201,
        )


@router.delete("/clients/{client_id}")
async def deactivate_client(client_id: int):
    """Deactivate a client token (soft-delete — row preserved for audit).

    Gated only by the shared HOME_UI_TOKEN — see list_clients for why.
    """
    db = get_db()
    with db.session() as session:
        client = session.query(ClientToken).filter_by(id=client_id).first()
        if not client:
            return JSONResponse({"error": "Client token not found"}, status_code=404)
        client.is_active = False
        session.commit()
        return {"status": "ok", "id": client_id}
