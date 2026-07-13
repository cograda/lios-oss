"""REST routes for Apple Reminders push sync.

The Mac agent calls these endpoints:
  POST /api/reminders/sync         — push full reminder state
  POST /api/reminders/backlog-sync — trigger vault <-> Reminders sync

The EventKit command dispatch/ack path (queue a command, SSE-push it to the
daemon, daemon acks) lives on the V3 API instead — see
app/integrations/apple_reminders/commands.py and
POST /api/v1/reminders/commands/{id}/done in app/api/v1.py. There is no
legacy GET /api/reminders/commands endpoint; despite the "legacy" label on
the reminder_commands table in server/CLAUDE.md, dispatch_command()/
complete_command() are the live implementation of the current SSE push
path, not a superseded one.
"""

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.auth.utils import safe_token_check
from app.config import settings
from app.db import get_db
from app.integrations.apple_reminders.models import Reminder
from app.integrations.apple_reminders.sync import sync_from_push

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reminders", tags=["reminders"])


def _check_token(authorization: str | None):
    """Verify the Mac agent is authenticated (constant-time compare)."""
    if not settings.mcp_token:
        return  # No auth configured
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not safe_token_check(token, settings.mcp_token):
        raise HTTPException(status_code=401, detail="Unauthorized")


# --- Push sync ---


class ReminderPush(BaseModel):
    uid: str
    list_name: str
    summary: str
    notes: str | None = None
    due_date: str | None = None
    priority: int = 0
    completed: bool = False
    completed_date: str | None = None
    flagged: bool = False


class SyncRequest(BaseModel):
    reminders: list[ReminderPush]


@router.post("/sync")
async def push_sync(body: SyncRequest, authorization: str | None = Header(None)):
    """Receive full reminder state from the Mac agent."""
    _check_token(authorization)

    # Legacy REST endpoint authed via shared MCP token, not per-user bearer.
    # Default to Alex (user_id=1). New per-user V3 path is /api/v1/reminders/push
    # which derives user_id from the bearer.
    db = get_db()
    with db.session() as session:
        count = sync_from_push(
            [r.model_dump() for r in body.reminders],
            session,
            user_id=1,
        )
    return {"status": "ok", "synced": count}


@router.post("/backlog-sync")
async def trigger_backlog_sync(authorization: str | None = Header(None)):
    """Manually trigger vault backlog <-> Reminders sync."""
    _check_token(authorization)

    if not settings.obsidian_vault_path:
        return {"status": "error", "message": "HOME_OBSIDIAN_VAULT_PATH not configured"}

    from app.integrations.apple_reminders.backlog_sync import sync_backlogs

    db = get_db()
    with db.session() as session:
        # sync_backlogs is a plain blocking function (file I/O + DB queries +
        # local fuzzy matching, no real async I/O) — bridge onto the event
        # loop so this manual-trigger request doesn't stall it.
        result = await asyncio.to_thread(
            sync_backlogs,
            session,
            settings.obsidian_vault_path,
        )
    logger.info(f"Backlog sync (manual): {result}")
    return {"status": "ok", **result}
