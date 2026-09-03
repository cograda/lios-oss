"""FastAPI application — home services hub."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

from app.auth.utils import safe_token_check
from app.config import settings
from app.db import get_db
import app.models  # noqa: F401 — ensure all models are registered before create_tables()
from app.integrations import register_all


def _check_model_registration() -> None:
    """Fail loud if any integrations/*/models.py isn't imported.

    Without this, forgetting to add a new integration's models to
    app/models/__init__.py silently skips table creation.
    """
    import sys
    from pathlib import Path
    integrations_dir = Path(__file__).resolve().parent / "integrations"
    missing = []
    for subdir in integrations_dir.iterdir():
        if not subdir.is_dir() or subdir.name.startswith("_") or subdir.name == "__pycache__":
            continue
        if not (subdir / "models.py").exists():
            continue
        module_name = f"app.integrations.{subdir.name}.models"
        if module_name not in sys.modules:
            missing.append(module_name)
    if missing:
        raise RuntimeError(
            f"Model registration check FAILED: {missing} not imported. "
            f"Add to app/models/__init__.py or tables won't be created."
        )


_check_model_registration()
from app.mcp.server import mcp_asgi_app, mcp_lifespan, register_mcp_tools
from app.scheduler import setup_scheduler, scheduler

logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, register MCP tools, start scheduler."""
    logger.info(f"Starting {settings.app_name}")
    # Init DB + create tables + run alembic migrations. Deliberately NOT
    # wrapped in try/except: a comar server without a database is worse
    # than a dead one (every tool returns confusing errors instead of the
    # container restarting and the failure being visible).
    get_db()
    register_all()

    # V4 chunk 1.1: fail loud at boot if any integration's manifest is
    # missing or inconsistent (bad model name, schedule drift, unknown/
    # cyclic depends_on, duplicate tool name). Pure declaration for now —
    # nothing else consumes these manifests yet.
    from app.plugin.validate import discover_manifests, validate_manifests
    from app.integrations import get_all as get_all_integrations
    validate_manifests(discover_manifests(), get_all_integrations())

    register_mcp_tools()
    setup_scheduler()

    # Capture the running loop so sync tool handlers (in asyncio.to_thread)
    # can publish SSE events back onto it via run_coroutine_threadsafe.
    from app.stream_manager import stream_manager
    stream_manager.loop = asyncio.get_running_loop()

    # Startup-kind background tasks, manifest-driven (V4 chunk 3.1): the
    # obsidian vault watcher and the Home Assistant WebSocket listener used
    # to be started here by name. Now every manifest's `background_tasks`
    # entries with kind="startup" are discovered, supervised (restart with
    # backoff on crash — see app.plugin.supervisor), and started as one
    # asyncio.Task each — this file never imports an integration package.
    from app.plugin.startup_tasks import start_all, stop_all
    startup_tasks, startup_stop_event = start_all()

    # Streamable HTTP MCP transport — task group must run for the app's lifetime.
    async with mcp_lifespan():
        yield

    await stop_all(startup_tasks, startup_stop_event)
    scheduler.shutdown(wait=False)
    logger.info("Shutdown complete")


# Schema changes live in alembic (run by get_db() on startup). The old
# _run_schema_migrations() inline-ALTER path was folded into revision
# f5a6b7c8d9e0; the one-time OAuth-token encryption hook is now
# app/scripts/encrypt_oauth_tokens.py (run manually if the key rotates).

app = FastAPI(title=settings.app_name, lifespan=lifespan)

# Paths that don't require UI auth.
# `/api/auth/google/login` MUST be exempt: when a refresh token dies, the
# user clicks the re-auth link from the dashboard banner. That link points
# at the Tailscale hostname (Google rejects `.lab` as a redirect URI), but
# the dashboard cookie is set on `comar.lab` and doesn't carry across
# domains. Gating this route would leave users with no way to recover.
# The route itself only builds a signed-state redirect to Google — the real
# auth happens at Google + the callback's HMAC state verification.
AUTH_EXEMPT = {
    "/api/health",
    "/api/auth/google/login",
    "/api/auth/google/callback",
    "/api/auth/login",
    "/api/auth/check",
    # An OAuth provider redirects the BROWSER back here, and `ui_token` is set
    # SameSite=Strict — so it is not sent on that cross-site navigation and the
    # callback would 401, losing a grant the user had just approved. Hence the
    # google callback above, and hence this one.
    #
    # Exempt from the UI token, NOT unauthenticated: both callbacks verify an
    # HMAC-signed `state` (strava's additionally expires after 10 minutes), so
    # a forged callback cannot attach a token to someone else's user row.
    #
    # ⚠️ This is the one place an integration cannot be dropped in without a
    # kernel edit. `MANIFEST.routes` mounts a router, but nothing in the
    # manifest can declare "this route authenticates itself", so a third-party
    # OAuth callback has to be named here by hand. Worth a `public_routes`
    # manifest field if a third provider ever appears.
    "/api/strava/callback",
}
# Prefix exemptions — routes under these prefixes handle their own auth
AUTH_EXEMPT_PREFIXES = ("/api/reminders/", "/api/client/", "/api/health/", "/api/inbox/", "/api/v1/", "/api/install/")


@app.middleware("http")
async def check_ui_auth(request: Request, call_next):
    """Gate API routes behind a simple token check (cookie or header).

    Fails CLOSED: an unset/empty `HOME_UI_TOKEN` used to fall straight
    through (the `and settings.ui_token` clause short-circuited the whole
    gate), leaving every UI-token-gated `/api/*` route — including
    `DELETE /api/data/purge/{integration}` — unauthenticated on a
    misconfigured deploy. Now a gated route with no configured token is
    rejected outright rather than treated as "auth not required".
    """
    path = request.url.path
    # Only protect /api/ routes (not static files, MCP, or exempt paths)
    if path.startswith("/api/") and path not in AUTH_EXEMPT and not any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
        if not settings.ui_token:
            return JSONResponse(
                {"error": "Service unavailable", "detail": "HOME_UI_TOKEN not configured"},
                status_code=503,
            )
        token = request.cookies.get("ui_token") or request.headers.get("X-UI-Token")
        if not safe_token_check(token, settings.ui_token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def api_security_headers(request: Request, call_next):
    """Small, blanket hardening for every `/api/*` response (V4 chunk 2.5).

    8400 is plaintext-by-design, LAN/Tailscale-only (see server/CLAUDE.md's
    transport-stance note) — these headers aren't about that; they're just
    good hygiene that costs nothing: stop browsers from MIME-sniffing
    responses into something executable, and stop any intermediate cache
    from persisting API responses (some of which carry bearer-adjacent data).
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
    return response


# API routes
from app.routes import router  # noqa: E402
app.include_router(router)

# V3 client API — bearer-token auth, used by comar-client and other surfaces.
from app.api.v1 import router as v1_router  # noqa: E402
app.include_router(v1_router)

# MCP endpoint — single ASGI app at /mcp that handles both SSE (/mcp/sse)
# and message POST (/mcp/messages). Mounted before the catch-all static mount.
app.router.routes.insert(0, Mount("/mcp", app=mcp_asgi_app))

# OAuth 2.1 authorization-server + protected-resource routes (claude.ai connector
# sign-in). Spliced ahead of the static catch-all, like the /mcp mount. These live
# at root (/authorize, /token, /register, /revoke, /oauth/login, /.well-known/*),
# so they sit outside the /api/* UI-token middleware and stay unauthenticated —
# the OAuth flow itself is the auth boundary. No-op if HOME_OAUTH_ISSUER is unset.
# See vault Plans/mcp-oauth.md.
from app.auth.oauth_wire import build_oauth_routes  # noqa: E402
for _oauth_route in build_oauth_routes():
    app.router.routes.insert(0, _oauth_route)

# In production, serve the Vite build output
# Works both locally (backend/../frontend/dist) and in Docker (/app/frontend/dist)
dist_dir = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if not dist_dir.is_dir():
    dist_dir = Path("/app/frontend/dist")  # Docker path
if dist_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="static")
