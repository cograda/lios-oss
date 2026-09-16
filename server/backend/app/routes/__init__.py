"""API routes.

Kernel routers only — auth, dashboard, integrations management, client
distribution, install, system, logs, data, inbox. Per-integration routers
(apple_reminders, apple_health, ...) are declared in each integration's
manifest (`routes: list[str]`, dotted refs to an `APIRouter` instance) and
mounted dynamically at startup by `app.plugin.routes.mount_integration_routes()`
(V4 chunk 3.1) — this module no longer imports any integration package.
"""

import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.db import get_db
from app.routes.auth import router as auth_router
from app.routes.dashboard import router as dashboard_router
from app.routes.integrations import router as integrations_router
from app.routes.client_dist import router as client_dist_router
from app.routes.install import router as install_router
from app.routes.system import router as system_router
from app.routes.logs import router as logs_router
from app.routes.data import router as data_router
from app.routes.inbox import router as inbox_router
from app.routes.preferences import router as preferences_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

router.include_router(auth_router)
router.include_router(dashboard_router)
router.include_router(integrations_router)
router.include_router(client_dist_router)
router.include_router(install_router)
router.include_router(system_router)
router.include_router(logs_router)
router.include_router(data_router)
router.include_router(inbox_router)
router.include_router(preferences_router)

# Per-integration routers, manifest-driven — mounted here (not imported
# above) so this module makes zero references to any integration package.
from app.plugin.routes import mount_integration_routes  # noqa: E402

mount_integration_routes(router)


@router.get("/health")
async def health():
    """Health check with database connectivity verification."""
    db_status = "ok"
    try:
        db = get_db()
        with db.session() as session:
            session.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {e}"
        logger.error(f"Health check DB failure: {e}")

    status = "ok" if db_status == "ok" else "degraded"
    return {"status": status, "db": db_status}
