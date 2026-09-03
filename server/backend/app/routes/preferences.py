"""Per-user preference management for the dashboard.

Mirrors `integrations.py`'s `/{name}/config` pair, but keyed by user rather
than by integration — see `app/models/user_preferences.py` for why the two
stores are separate.

Unlike integration config there is nothing secret here, so values round-trip
unmasked. These routes sit on the UI-token-gated `/api/*` surface like the
rest of the dashboard.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.db import SessionDep
from app.models.users import User
from app.services import preferences as prefs_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/preferences", tags=["preferences"])


def _require_user(session, user_id: int) -> User:
    user = session.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(404, f"User {user_id} not found")
    return user


@router.get("/schema")
async def get_preferences_schema():
    """The registry: every key, its type, default, description and group.

    Lets the dashboard render the editor without hardcoding the key list —
    a preference added to `app.services.preferences.PREFERENCES` shows up in
    the UI with no frontend change.
    """
    return {"preferences": prefs_service.schema()}


@router.get("/{user_id}")
async def get_user_preferences(user_id: int, session: SessionDep):
    """Resolved preferences for one user — stored values over defaults."""
    _require_user(session, user_id)
    return {"user_id": user_id, "preferences": prefs_service.get_all(session, user_id)}


@router.put("/{user_id}")
async def put_user_preferences(user_id: int, request: Request, session: SessionDep):
    """Upsert preferences for one user.

    Body: `{"key": value, ...}`. Unknown keys and values that don't match the
    declared type are rejected with a 400 naming the problem — a write must
    fail loudly rather than be accepted and then silently ignored by the read
    path's default fallback.
    """
    _require_user(session, user_id)

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object of key -> value")

    try:
        written = prefs_service.set_many(session, user_id, body)
    except KeyError as e:
        raise HTTPException(400, str(e).strip("'")) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # Preferences shape what the daily brief fetches and how, so a stale
    # cached payload would keep the old behaviour until the TTL expired —
    # confusing right after someone changes a setting and re-runs /refresh.
    # Via the facade, not `system.brief`: this is kernel code, and
    # `tests/test_kernel_import_guard.py` allows crossing into an integration
    # only through `<pkg>.facade`.
    try:
        from app.integrations.system.facade import FACADE as system_facade

        system_facade.clear_brief_cache(user_id)
    except Exception:  # noqa: BLE001
        logger.exception("failed to clear daily-brief cache after a preference change")

    return {"user_id": user_id, "updated": written}
