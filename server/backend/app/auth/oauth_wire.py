"""Wire the SDK's OAuth AS + RS routes and our login funnel into FastAPI.

`build_oauth_routes()` returns Starlette routes the app splices ahead of the
static catch-all (like the /mcp mount). No-op unless HOME_OAUTH_ISSUER is set.

Phase 0 login is a UI-token gate — throwaway scaffolding to prove the handshake
against the real claude.ai client before wiring Google federation (Phase 1).
See vault Plans/mcp-oauth.md.

V4 chunk 2.3 added an explicit kill switch: Phase 0 login used to accept
any bearer matching `HOME_UI_TOKEN` and mint a session for user_id=1 no
matter what — a standing impersonation path once `HOME_OAUTH_ISSUER` is
set for real use. It now also requires `settings.oauth_phase0_enable`
(`HOME_OAUTH_PHASE0_ENABLE`), off by default. This is a config flag rather
than deleting the code outright because Phase 1 (Google federation)
replaces the login step in place — the AS/RS routes, token minting, and
refresh/revocation machinery below are already production-shaped and
don't change; only `_login_post` needs to go away once Phase 1 lands.
"""

import logging

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp.server.auth.routes import (
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

from app.auth.oauth_provider import provider
from app.auth.utils import safe_token_check
from app.config import settings

logger = logging.getLogger(__name__)

# Phase 0: the UI-token gate authenticates as Alex (user_id=1). Phase 1 replaces
# this whole route pair with Google-federated login + email→user mapping.
_PHASE0_USER_ID = 1

_LOGIN_FORM = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to Comar</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
form{{background:#1c1c1e;padding:2rem;border-radius:14px;width:min(360px,90vw)}}
h1{{font-size:1.1rem;margin:0 0 1rem}}
input{{width:100%;box-sizing:border-box;padding:.7rem;border-radius:9px;
border:1px solid #333;background:#000;color:#eee;font-size:1rem}}
button{{width:100%;margin-top:1rem;padding:.7rem;border:0;border-radius:9px;
background:#0a84ff;color:#fff;font-size:1rem;font-weight:600}}
.err{{color:#ff453a;font-size:.85rem;margin-top:.6rem}}</style></head>
<body><form method="post" action="/oauth/login">
<h1>Sign in to Comar</h1>
<input type="hidden" name="login_session" value="{session}">
<input type="password" name="ui_token" placeholder="Comar UI token" autofocus
 autocomplete="current-password">
{error}
<button type="submit">Authorise</button>
</form></body></html>"""


async def _login_get(request: Request) -> Response:
    session_id = request.query_params.get("login_session", "")
    if not session_id:
        return HTMLResponse("Missing login_session", status_code=400)
    return HTMLResponse(_LOGIN_FORM.format(session=session_id, error=""))


async def _login_post(request: Request) -> Response:
    form = await request.form()
    session_id = str(form.get("login_session", ""))
    ui_token = str(form.get("ui_token", ""))
    if not session_id:
        return HTMLResponse("Missing login_session", status_code=400)
    if not settings.oauth_phase0_enable:
        logger.warning("OAuth Phase-0 login attempted while disabled (HOME_OAUTH_PHASE0_ENABLE=false)")
        return HTMLResponse(
            "Phase 0 sign-in is disabled on this server.",
            status_code=403,
        )
    if not (settings.ui_token and safe_token_check(ui_token, settings.ui_token)):
        return HTMLResponse(
            _LOGIN_FORM.format(
                session=session_id, error='<div class="err">Invalid token</div>'
            ),
            status_code=401,
        )
    redirect_url = provider.complete_login(session_id, _PHASE0_USER_ID)
    if not redirect_url:
        return HTMLResponse(
            "Login session expired — restart the connection from the client.",
            status_code=400,
        )
    return RedirectResponse(redirect_url, status_code=302)


def build_oauth_routes() -> list[Route]:
    """All OAuth AS + RS + login routes, or [] if HOME_OAUTH_ISSUER is unset."""
    issuer = settings.oauth_issuer.strip()
    if not issuer:
        logger.info("OAuth AS disabled — HOME_OAUTH_ISSUER not set")
        return []

    issuer_url = AnyHttpUrl(issuer)
    resource_url = AnyHttpUrl(issuer.rstrip("/") + "/mcp")

    routes: list[Route] = list(
        create_auth_routes(
            provider=provider,
            issuer_url=issuer_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        )
    )
    routes += create_protected_resource_routes(
        resource_url=resource_url,
        authorization_servers=[issuer_url],
        resource_name="Comar MCP",
    )
    routes += [
        Route("/oauth/login", endpoint=_login_get, methods=["GET"]),
        Route("/oauth/login", endpoint=_login_post, methods=["POST"]),
    ]
    logger.info("OAuth AS enabled — issuer=%s, %d routes", issuer, len(routes))
    return routes
