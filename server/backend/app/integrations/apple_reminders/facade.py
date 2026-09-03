"""apple_reminders's declared facade — capability `reminders.query` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.apple_reminders`. Currently one consumer: `system`'s
morning-briefing/week-ahead composites.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.apple_reminders.tools import (
    handle_list_reminders,
    handle_sync_reminders,
)


class AppleRemindersFacade:
    def list_reminders(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_list_reminders(session, arguments)

    def sync(self, session: Session, arguments: dict[str, Any]) -> str:
        """Open reminders *plus* what changed since `since`.

        The daily brief prefers this over `list_reminders` for the same reason
        the command template does: items ticked off on the phone since the last
        note are otherwise silently dropped. It also carries
        `bridge_verified_at`, which is the only trustworthy daemon-liveness
        signal (`synced_at` legitimately sits still on quiet days).
        """
        return handle_sync_reminders(session, arguments)

    def pending_writes(self, session: Session) -> list[dict[str, Any]]:
        """Per-user count and age of EventKit writes still waiting to be applied.

        The read half of the write-channel alert axis. Returns
        `[{"user_id", "user_name", "count", "oldest_age_seconds"}]`, omitting
        users with nothing queued.

        On the facade because `system` may not import this package's models
        (`tests/test_capability_boundaries.py`), and because the caller needs
        `max_replay_age_seconds` too — the threshold and the data have to come
        from the same place or the alert text can drift from the behaviour.
        """
        from datetime import datetime, timezone

        from sqlalchemy import func

        from app.integrations.apple_reminders.models import ReminderCommand
        from app.models.users import User

        now = datetime.now(timezone.utc)
        rows = (
            session.query(
                ReminderCommand.user_id,
                User.name,
                func.count(ReminderCommand.id),
                func.min(ReminderCommand.created_at),
            )
            .join(User, User.id == ReminderCommand.user_id)
            .filter(ReminderCommand.status == "pending")
            .group_by(ReminderCommand.user_id, User.name)
            .all()
        )
        out = []
        for user_id, user_name, count, oldest in rows:
            if oldest is not None and oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            out.append({
                "user_id": user_id,
                "user_name": user_name,
                "count": count,
                "oldest_age_seconds": int((now - oldest).total_seconds()) if oldest else None,
            })
        return out

    @property
    def max_replay_age_seconds(self) -> int:
        """How long a queued write stays eligible for retry before expiring."""
        from app.integrations.apple_reminders.commands import MAX_REPLAY_AGE

        return int(MAX_REPLAY_AGE.total_seconds())

    def drain_pending_commands(self, *, user_id: int, user_name: str) -> dict[str, int]:
        """Retry EventKit writes queued while this user's daemon was unreachable.

        Exposed on the facade because the caller is `app/api/v1.py`'s SSE
        subscribe handler — kernel code, which `tests/test_kernel_import_guard.py`
        forbids from importing `apple_reminders.commands` directly. The capability
        boundary is the only legal route.

        Opens its own session rather than taking one: the caller runs this in a
        worker thread off the SSE path, and threading a request-scoped session
        into another thread is how you get a session used from two threads at
        once.
        """
        from app.db import get_db
        from app.integrations.apple_reminders.commands import drain_pending

        with get_db().session() as session:
            return drain_pending(session, user_id=user_id, user_name=user_name)


FACADE = AppleRemindersFacade()
