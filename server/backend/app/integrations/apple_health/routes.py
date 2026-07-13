"""REST endpoint for Apple Health data from Health Auto Export iOS app.

Health Auto Export pushes the same JSON format as its file export via
HTTP POST. This endpoint parses it and feeds it through the existing
sync_from_push() pipeline.

Configure in Health Auto Export app:
  URL: http://<server>:8400/api/health/push
  Method: POST
  Headers: Authorization: Bearer <HOME_UI_TOKEN>
  Format: JSON
"""

import logging

from fastapi import APIRouter, Request, Response

from app.config import HomeSettings
from app.db import get_db
from app.integrations.apple_health.parse_export import parse_health_auto_export
from app.integrations.apple_health.sync import sync_from_push

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

settings = HomeSettings()


def _check_auth(request: Request) -> bool:
    """Verify Authorization: Bearer <token> against health push token or UI token.

    Accepts HOME_HEALTH_PUSH_TOKEN (preferred, least-privilege) or
    HOME_UI_TOKEN (fallback, for initial setup convenience).
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.headers.get("X-UI-Token", "")
    if not token:
        return False
    from app.auth.utils import safe_token_check
    # Check dedicated health token first
    if settings.health_push_token and safe_token_check(token, settings.health_push_token):
        return True
    # Fall back to UI token
    if settings.ui_token and safe_token_check(token, settings.ui_token):
        return True
    return False


@router.post("/push")
async def push_health(request: Request):
    """Receive Health Auto Export JSON and sync to database.

    Accepts the raw JSON payload from the Health Auto Export iOS app.
    Parses metrics, workouts, and sleep data, then upserts into Postgres.
    """
    if not _check_auth(request):
        return Response(
            content='{"error": "Unauthorized"}',
            status_code=401,
            media_type="application/json",
        )

    try:
        raw = await request.json()
    except Exception:
        return Response(
            content='{"error": "Invalid JSON"}',
            status_code=400,
            media_type="application/json",
        )

    try:
        daily_metrics, workouts, sleep_sessions = parse_health_auto_export(raw)
    except Exception as e:
        logger.exception("Failed to parse Health Auto Export JSON")
        return Response(
            content=f'{{"error": "Parse error: {e}"}}',
            status_code=422,
            media_type="application/json",
        )

    # Health Auto Export auths via shared token, not per-user bearer, so the
    # endpoint can't infer user identity. Default to Alex (user_id=1). Sam's
    # iPhone push will need its own dedicated route or a path-prefixed token
    # — see Phase E in .claude/plans/multi-user-e2e.md.
    user_id = 1

    try:
        db = get_db()
        with db.session() as session:
            count = sync_from_push(
                daily_metrics, workouts, sleep_sessions,
                user_id=user_id, session=session,
            )
    except Exception as e:
        logger.exception("Failed to sync health data to database")
        return Response(
            content=f'{{"error": "Sync error: {e}"}}',
            status_code=500,
            media_type="application/json",
        )

    logger.info(
        f"Health REST push: {len(daily_metrics)} metrics, "
        f"{len(workouts)} workouts, {len(sleep_sessions)} sleep → {count} synced"
    )

    # Bump SyncState so freshness alerts see the integration as alive. The
    # v1 push endpoint already does this; this legacy endpoint did not, so
    # an iOS shortcut configured against /api/health/push left the alerting
    # layer blind even when data was flowing.
    from app.scheduler import _update_sync_state
    _update_sync_state("apple_health", status="ok", trigger="push")

    return {
        "ok": True,
        "count": count,
        "daily_metrics": len(daily_metrics),
        "workouts": len(workouts),
        "sleep_sessions": len(sleep_sessions),
    }
