"""API routes."""

import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.db import get_db
from app.routes.auth import router as auth_router
from app.routes.dashboard import router as dashboard_router
from app.routes.integrations import router as integrations_router
from app.integrations.apple_reminders.routes import router as reminders_router
from app.integrations.apple_health.routes import router as health_router
from app.routes.client_dist import router as client_dist_router
from app.routes.install import router as install_router
from app.routes.system import router as system_router
from app.routes.logs import router as logs_router
from app.routes.data import router as data_router
from app.routes.inbox import router as inbox_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

router.include_router(auth_router)
router.include_router(dashboard_router)
router.include_router(integrations_router)
router.include_router(reminders_router)
router.include_router(health_router)
router.include_router(client_dist_router)
router.include_router(install_router)
router.include_router(system_router)
router.include_router(logs_router)
router.include_router(data_router)
router.include_router(inbox_router)


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
