"""REST routes for Apple Reminders backlog sync.

The Mac agent calls:
  POST /api/reminders/backlog-sync — trigger vault <-> Reminders sync

The EventKit command dispatch/ack path (queue a command, SSE-push it to the
daemon, daemon acks) lives on the V3 API instead — see
app/integrations/apple_reminders/commands.py and
POST /api/v1/reminders/commands/{id}/done in app/api/v1.py. There is no
legacy GET /api/reminders/commands endpoint; despite the "legacy" label on
the reminder_commands table in server/CLAUDE.md, dispatch_command()/
complete_command() are the live implementation of the current SSE push
path, not a superseded one.

sam-rollout A2 (2026-07-26): this route used to gate on the shared
`HOME_UI_TOKEN` secret and hardcode user_id=1 (it predates client_tokens).
It now resolves the caller via the same per-user bearer dependency the V3
API uses (`app.auth.client_token.get_current_user`) and rejects (401) rather
than defaulting.

The legacy `POST /api/reminders/sync` push-sync route (the other half of
this pair, superseded by the per-user `/api/v1/reminders/push` the client
daemon actually calls) was removed 2026-08-08 as confirmed dead code —
nothing in client/ or server/ posted to it.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends

from app.db import get_db
from app.models.users import User
from app.auth.client_token import get_current_user
from app.services import vault_paths

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reminders", tags=["reminders"])


@router.post("/backlog-sync")
async def trigger_backlog_sync(user: User = Depends(get_current_user)):
    """Manually trigger vault backlog <-> Reminders sync for the calling user."""
    vault_dir = vault_paths.user_vault_path(user.name)
    if not vault_dir.is_dir():
        return {"status": "error", "message": f"No vault directory for {user.name}"}

    from app.integrations.apple_reminders.backlog_sync import sync_backlogs

    db = get_db()
    with db.session() as session:
        # sync_backlogs is a plain blocking function (file I/O + DB queries +
        # local fuzzy matching, no real async I/O) — bridge onto the event
        # loop so this manual-trigger request doesn't stall it.
        result = await asyncio.to_thread(
            sync_backlogs,
            session,
            str(vault_dir),
            user.id,
        )
    logger.info(f"Backlog sync (manual) [{user.name}]: {result}")
    return {"status": "ok", **result}
