"""REST endpoint for Apple Health data from Health Auto Export iOS app.

Health Auto Export pushes the same JSON format as its file export via
HTTP POST. This endpoint parses it and feeds it through the existing
sync_from_push() pipeline.

Configure in Health Auto Export app:
  URL: https://<host>.<tailnet>.ts.net/api/health/push
  Method: POST
  Headers: Authorization: Bearer <per-user client_token>
  Format: JSON
  Window: a *trailing* window (7 days), not "since last export"

Two deliberate choices there, both about surviving a phone that isn't on the
home Wi-Fi:

1. **The tailnet URL, not the LAN IP.** The iOS app can only be pointed at one
   URL, so it has to be one that resolves from anywhere — which means Tailscale
   stays on on the phone. A LAN IP silently fails every export made outside the
   house, and (worse) on a foreign 192.168.1.x network may reach some *other*
   device entirely. The Mac client solves the same problem differently, by
   probing a preference list — see `client/src/comar/endpoints.py` — but that
   option isn't open to a third-party iOS app.

2. **A trailing window rather than an incremental cursor.** Any export made
   while Tailscale is down is simply lost; with a 7-day window the next
   successful export re-sends it and the hole repairs itself. `sync_from_push`
   upserts on `(user_id, date, metric_type)` and `(user_id, uid)`, so re-sending
   is free — no dedup logic, no duplicate rows.

Gaps longer than the window still need noticing, and the manifest's staleness
probe won't do it: it reads MAX(synced_at), which a re-send keeps green. That's
what `facade.coverage_gaps()` is for — it looks for holes in the `date` axis,
per user, and `system`'s alerts payload surfaces them.

sam-rollout A2 (2026-07-26): this endpoint used to accept a shared secret
(HOME_HEALTH_PUSH_TOKEN or HOME_UI_TOKEN) and hardcode user_id=1, since it
predates client_tokens. It now resolves the caller from the same per-user
bearer the V3 API uses (`app.auth.client_token.get_current_user`) and
rejects (401) rather than defaulting — see server/CLAUDE.md and the
sam-rollout plan for why defaulting silently misattributed data. This is
a breaking change for any caller still sending the old shared secret: it
must switch to a real `client_tokens` bearer before this deploys.
"""

import logging

from fastapi import APIRouter, Depends, Request, Response

from app.auth.client_token import get_current_user
from app.db import get_db
from app.integrations.apple_health.parse_export import parse_health_auto_export
from app.integrations.apple_health.sync import sync_from_push
from app.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


def _record_push(status: str, error: str | None = None) -> None:
    """Write the outcome of a push attempt to SyncState.

    Every exit from the push route goes through here, success or failure, and
    that totality is the point. Before this, only the success path wrote
    SyncState, so `apple_health.consecutive_failures` could never leave 0 and
    `last_error` could never be non-null - which made a phone sending payloads
    we reject indistinguishable from a phone not calling at all. Both rendered
    as `status: "ok"`, with a stale-data warning arriving 36 hours later.

    Axis 1 of `system.alerts` fires on `consecutive_failures >= 1`, so simply
    recording the failure is enough to surface it; no new alerting code needed.

    Recording *attempts* also gives `last_sync_at` a second, sharper meaning for
    a push_source: the last time the source tried. `facade.push_silence()` reads
    it to catch a phone that has gone quiet, which lands hours before the
    data-staleness threshold does.

    Bookkeeping must never fail a push that otherwise worked, hence the guard.
    """
    try:
        from app.scheduler import _update_sync_state
        _update_sync_state("apple_health", status=status, error=error, trigger="push")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record apple_health push state")


@router.post("/push")
async def push_health(request: Request, user: User = Depends(get_current_user)):
    """Receive Health Auto Export JSON and sync to database.

    Accepts the raw JSON payload from the Health Auto Export iOS app.
    Parses metrics, workouts, and sleep data, then upserts into Postgres,
    attributed to the bearer's resolved user (`get_current_user` raises
    401 before this body runs if the token is missing/invalid/expired).
    """
    try:
        raw = await request.json()
    except Exception:
        _record_push("error", "invalid JSON body")
        return Response(
            content='{"error": "Invalid JSON"}',
            status_code=400,
            media_type="application/json",
        )

    try:
        daily_metrics, workouts, sleep_sessions, skipped = parse_health_auto_export(raw)
    except Exception as e:
        logger.exception("Failed to parse Health Auto Export JSON")
        _record_push("error", f"parse error: {e}")
        return Response(
            content=f'{{"error": "Parse error: {e}"}}',
            status_code=422,
            media_type="application/json",
        )

    user_id = user.id

    try:
        db = get_db()
        with db.session() as session:
            count = sync_from_push(
                daily_metrics, workouts, sleep_sessions,
                user_id=user_id, session=session,
            )
    except Exception as e:
        logger.exception("Failed to sync health data to database")
        _record_push("error", f"sync error: {e}")
        return Response(
            content=f'{{"error": "Sync error: {e}"}}',
            status_code=500,
            media_type="application/json",
        )

    logger.info(
        f"Health REST push: {len(daily_metrics)} metrics, "
        f"{len(workouts)} workouts, {len(sleep_sessions)} sleep -> {count} synced"
        + (f" ({len(skipped)} records skipped)" if skipped else "")
    )

    # The parser now drops unreadable records rather than 422-ing the whole
    # export (see parse_export.py), so skips have to be recorded somewhere or a
    # permanently malformed record is dropped silently on every re-send forever
    # - trading a loud failure for a quiet one, the worse of the two.
    #
    # Recorded as status="ok" *with* an error string, deliberately. Any status
    # other than "ok" increments `consecutive_failures`, which axis 1 of
    # `system.alerts` renders as "failing (Nx consecutive)" - and this sync did
    # not fail, it succeeded while skipping a row. Marking it failing would
    # send someone to debug an outage that isn't happening, the same
    # mis-signalling the lastfm "no new data at the source" fix was about.
    # `last_error` is written regardless of status, so the detail still reaches
    # the dashboard and SyncHistory without faking an outage.
    _record_push(
        "ok",
        f"{len(skipped)} unreadable records skipped: " + "; ".join(skipped[:3])
        if skipped else None,
    )

    return {
        "ok": True,
        "count": count,
        "daily_metrics": len(daily_metrics),
        "workouts": len(workouts),
        "sleep_sessions": len(sleep_sessions),
        "skipped": skipped,
    }
