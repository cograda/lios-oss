"""Wire the SDK's OAuth AS + RS routes and our login funnel into FastAPI.

`build_oauth_routes()` returns Starlette routes the app splices ahead of the
static catch-all (like the /mcp mount). No-op unless HOME_OAUTH_ISSUER is set.

**The login step takes the per-user bearer (2026-09-06 — one credential).**
`/oauth/login` is where the SDK's `authorize` handler parks a claude.ai (or
any other MCP client) connection and sends the human to prove who they are.
Until 2026-09-06 that proof was the shared dashboard token, and whoever typed
it became user_id=1 (the Phase-0 login, deleted in PR #123). Now the form
asks for the person's own `client_tokens` bearer, resolves it exactly as the
HTTP API and MCP transport do (`resolve_token_to_user` — active user,
unexpired bearer), and completes the login *as that user*. There is no
default user and no other accepted credential; a wrong token is a 401 and
spends the same per-IP failure budget as every other bearer path.

The AS/RS routes, token minting, refresh and revocation are the SDK's and the
provider's, unchanged.
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
from app.config import settings

logger = logging.getLogger(__name__)

_LOGIN_FORM = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to lios</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
form{{background:#1c1c1e;padding:2rem;border-radius:14px;width:min(380px,90vw)}}
h1{{font-size:1.1rem;margin:0 0 .6rem}}
p{{font-size:.85rem;color:#aaa;margin:0 0 1rem;line-height:1.45}}
code{{color:#ccc}}
input{{width:100%;box-sizing:border-box;padding:.7rem;border-radius:9px;
border:1px solid #333;background:#000;color:#eee;font-size:1rem}}
button{{width:100%;margin-top:1rem;padding:.7rem;border:0;border-radius:9px;
background:#0a84ff;color:#fff;font-size:1rem;font-weight:600}}
.err{{color:#ff453a;font-size:.85rem;margin-top:.6rem}}</style></head>
<body><form method="post" action="/oauth/login">
<h1>Sign in to lios</h1>
<p>Paste your lios token &mdash; the one the install script put in
<code>~/.config/lios/config.toml</code> under <code>[server] token</code>.
No token? Ask the admin for one.</p>
<input type="hidden" name="login_session" value="{session}">
<input type="password" name="token" placeholder="Your lios token" autofocus
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
    """Resolve the posted per-user bearer and complete the parked login as
    that user. No default user: an unresolvable token is a 401 and the form
    again, nothing else."""
    from app.auth.client_token import TokenExpiredError, resolve_token_to_user
    from app.auth.hashing import token_last4
    from app.auth.rate_limit import is_over_limit, record_failure

    client_ip = request.client.host if request.client else "unknown"
    if is_over_limit(client_ip):
        return HTMLResponse("Too many attempts — slow down.", status_code=429)

    form = await request.form()
    session_id = str(form.get("login_session", ""))
    token = str(form.get("token", "")).strip()
    if not session_id:
        return HTMLResponse("Missing login_session", status_code=400)

    user = None
    if token:
        try:
            user = resolve_token_to_user(token)
        except TokenExpiredError:
            user = None
    if user is None:
        record_failure(client_ip)
        logger.warning(
            "oauth login failed (invalid, expired or missing bearer): last4=%s ip=%s",
            token_last4(token), client_ip,
        )
        return HTMLResponse(
            _LOGIN_FORM.format(
                session=session_id, error='<div class="err">Invalid token</div>'
            ),
            status_code=401,
        )

    redirect_url = provider.complete_login(session_id, user.id)
    if not redirect_url:
        return HTMLResponse(
            "Login session expired — restart the connection from the client.",
            status_code=400,
        )
    logger.info("oauth login completed for user_id=%s", user.id)
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
