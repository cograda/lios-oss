"""Strava OAuth connect/callback routes.

Strava is a third-party OAuth2 provider, so it cannot ride `app/auth/oauth.py`:
that module is Google-specific by construction — its `_oauth_scopes()` builds
the union of every manifest's `oauth.scopes` and requests them all on one
Google consent screen. The manifest's `oauth` field means "Google scopes this
integration needs", which is why this integration declares `oauth=None` and
mounts its own two routes via `MANIFEST.routes` instead. No kernel edit.

What IS reused is the HMAC state signing, deliberately rather than
reimplemented: `state` is attacker-controllable on the way back in, and a
second, subtly different signing scheme in the codebase is a second chance to
get one wrong.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth.encryption import encrypt_token
from app.auth.oauth import _sign_state, _verify_state
from app.auth.ui_session import current_ui_user
from app.config import settings
from app.db import get_db
from app.integrations.strava import client as strava_client
from app.integrations.strava.sync import PROVIDER, _credentials
from app.models.tokens import OAuthToken
from app.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/strava", tags=["strava"])

#: How long a signed `state` stays valid. The consent screen is a handful of
#: clicks; ten minutes is generous and bounds replay of a leaked state value.
STATE_TTL_SECONDS = 600


def _redirect_uri(request: Request) -> str:
    """The callback URL, which must match Strava's registered callback domain.

    Prefers `oauth_redirect_base` for the same reason the Google flow does:
    the server's own address is a private LAN IP or a tailnet name, and
    Strava validates the callback against the *Authorization Callback Domain*
    registered on the API application.
    """
    if settings.oauth_redirect_base:
        return f"{settings.oauth_redirect_base.rstrip('/')}/api/strava/callback"
    return str(request.url_for("strava_callback"))


@router.get("/connect")
async def strava_connect(
    request: Request, user: str, caller: User = Depends(current_ui_user),
):
    """Start the Strava OAuth flow. Visit in a browser.

    `?user=` is REQUIRED and names the User row the resulting token is
    attributed to — there is no default. The Google flow removed its default
    after a re-auth performed during one person's setup was silently
    attributed to the other; the same trap applies exactly here, so the same
    rule does. And since 2026-09-06 the signed-in caller may only name
    themselves unless they are an admin — starting a grant against someone
    else's row is an admin act.

    Example: /api/strava/connect?user=alex
    """
    if not caller.is_admin and caller.name != user:
        return JSONResponse(
            {"error": "you may only connect Strava for your own user"}, status_code=403,
        )
    try:
        client_id, _ = _credentials()
    except Exception as exc:  # PermanentError names the missing keys
        return JSONResponse({"error": str(exc)}, status_code=400)

    db = get_db()
    with db.session() as session:
        if not session.query(User).filter_by(name=user).first():
            return JSONResponse(
                {"error": f"unknown user '{user}' — must exist in users table"},
                status_code=400,
            )

    state = _sign_state({
        "user": user,
        "provider": PROVIDER,
        "iat": int(datetime.now(timezone.utc).timestamp()),
    })
    return RedirectResponse(
        strava_client.build_authorize_url(
            client_id=client_id, redirect_uri=_redirect_uri(request), state=state
        )
    )


@router.get("/callback", name="strava_callback")
async def strava_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    scope: str | None = None,
    error: str | None = None,
):
    """Exchange the authorization code and store the token.

    ⚠️ The granted `scope` is verified, not assumed. Strava's consent screen
    lets the athlete untick individual permissions, and a token missing
    `activity:read_all` still works — it just silently returns fewer
    activities, with no error anywhere. Refusing the connection here is the
    only place that omission is visible; accepted quietly it would surface
    months later as "some of my rides never came across".
    """
    if error:
        return JSONResponse({"error": f"Strava returned: {error}"}, status_code=400)
    if not code or not state:
        return JSONResponse({"error": "missing code or state"}, status_code=400)

    try:
        payload = _verify_state(state)
    except ValueError as exc:
        return JSONResponse({"error": f"invalid state: {exc}"}, status_code=400)

    issued_at = payload.get("iat", 0)
    if int(datetime.now(timezone.utc).timestamp()) - issued_at > STATE_TTL_SECONDS:
        return JSONResponse({"error": "state expired — restart at /api/strava/connect"}, status_code=400)

    granted = set((scope or "").split(","))
    if "activity:read_all" not in granted:
        return JSONResponse(
            {
                "error": "Strava did not grant activity:read_all",
                "granted": sorted(granted),
                "detail": (
                    "Without this scope private and 'Only You' activities are "
                    "omitted from every response, with no error — the archive "
                    "would be silently incomplete. Restart at "
                    "/api/strava/connect and leave all boxes ticked."
                ),
            },
            status_code=400,
        )

    client_id, client_secret = _credentials()
    tokens = strava_client.exchange_code(
        client_id=client_id, client_secret=client_secret, code=code
    )

    athlete = tokens.get("athlete") or {}
    athlete_id = str(athlete.get("id") or "unknown")

    db = get_db()
    with db.session() as session:
        user = session.query(User).filter_by(name=payload["user"]).first()
        if user is None:
            return JSONResponse({"error": "user no longer exists"}, status_code=400)

        # `account_email` holds the Strava athlete id, not an email: it is the
        # column the (user_id, provider, account_email) uniqueness is built
        # on, and the athlete id is the only stable identifier Strava gives
        # us. An athlete's display name changes; their id does not.
        row = (
            session.query(OAuthToken)
            .filter_by(provider=PROVIDER, user_id=user.id, account_email=athlete_id)
            .first()
        )
        if row is None:
            row = OAuthToken(
                provider=PROVIDER, user_id=user.id, account_email=athlete_id
            )
            session.add(row)

        row.access_token = encrypt_token(tokens["access_token"])
        row.refresh_token = encrypt_token(tokens["refresh_token"])
        row.token_type = tokens.get("token_type", "Bearer")
        row.scopes = scope
        row.expires_at = datetime.fromtimestamp(tokens["expires_at"], tz=timezone.utc)
        row.needs_reauth_at = None
        row.needs_reauth_reason = None
        session.commit()

        athlete_name = " ".join(
            part for part in (athlete.get("firstname"), athlete.get("lastname")) if part
        )
        logger.info("Strava: connected athlete %s for user %s", athlete_id, user.name)

        return {
            "status": "ok",
            "user": user.name,
            "athlete_id": athlete_id,
            "athlete": athlete_name or None,
            "scopes": scope,
            "message": (
                f"Strava connected for {user.name}. Run the `strava_backfill` "
                "MCP tool to pull your full history. You can close this tab."
            ),
        }
