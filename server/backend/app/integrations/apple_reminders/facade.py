"""apple_reminders's declared facade — capabilities `reminders.query` (V4
chunk 4.2) and `reminders.write` (E chunk 6b, 2026-09-04).

The only surface another integration is allowed to import from
`app.integrations.apple_reminders`. Consumers: `system`'s morning-briefing/
week-ahead composites (`reminders.query`), and `tasks.reminders_inlet`'s
periodic tick (both — see that module for the full reminders-as-inlet
contract that retired `/reconcile-reminders`).
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

        Counts `draining` alongside `pending` — a command claimed by
        `drain_pending` for dispatch is still an outstanding write, not a
        resolved one, until it lands as `superseded`/`done`/`failed`. Treating
        it as invisible here is exactly how a claim stranded by a mid-dispatch
        process death (a deploy restart, an OOM kill, a container recreate)
        would go unnoticed by the one alert axis built to catch a silently
        undelivered write — see `commands.py::DRAINING_CLAIM_TIMEOUT`.
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
            .filter(ReminderCommand.status.in_(("pending", "draining")))
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

    # ─── reminders-inlet surface (E chunk 6b) ───────────────────────────────
    #
    # `tasks.reminders_inlet` is the only consumer of everything below. It
    # needs plumbing `list_reminders`/`sync` don't offer: reading and
    # writing the row-level `linked_task_uid` link, and dispatching a
    # completion without going through an MCP tool call. Kept on this same
    # facade (rather than a second `reminders_write.py`) because it is the
    # same underlying table and the same write channel — the split that
    # matters is `reminders.query` vs `reminders.write` as *capability
    # names* `depends_on` can select between, not as separate modules.

    def open_unlinked(self, session: Session, user_id: int) -> list[dict[str, Any]]:
        """Open reminders for `user_id` with no ledger task yet — rule 1's
        input. Returns plain dicts (`uid`, `summary`, `notes`, `due_date`),
        never `Reminder` ORM rows, so the caller can't reach for a column
        this contract doesn't cover."""
        from app.integrations.apple_reminders.models import Reminder

        rows = (
            session.query(Reminder)
            .filter(
                Reminder.user_id == user_id,
                Reminder.completed == False,  # noqa: E712
                Reminder.linked_task_uid.is_(None),
            )
            .all()
        )
        return [
            {"uid": r.uid, "summary": r.summary, "notes": r.notes, "due_date": r.due_date}
            for r in rows
        ]

    def link_task(self, session: Session, *, reminder_uid: str, user_id: int, task_uid: str) -> None:
        """Record the durable link after a capture. Set once, never cleared
        — see `Reminder.linked_task_uid`'s column comment for why."""
        from app.integrations.apple_reminders.models import Reminder

        row = (
            session.query(Reminder)
            .filter(Reminder.user_id == user_id, Reminder.uid == reminder_uid)
            .one_or_none()
        )
        if row is not None:
            row.linked_task_uid = task_uid
            session.commit()

    def linked_open(self, session: Session, user_id: int) -> list[dict[str, Any]]:
        """Linked reminders still open — rule 2's input: candidates whose
        ledger task may now be done/dropped and need a completion pushed."""
        from app.integrations.apple_reminders.models import Reminder

        rows = (
            session.query(Reminder)
            .filter(
                Reminder.user_id == user_id,
                Reminder.completed == False,  # noqa: E712
                Reminder.linked_task_uid.isnot(None),
            )
            .all()
        )
        return [{"uid": r.uid, "task_uid": r.linked_task_uid} for r in rows]

    def linked_completed(self, session: Session, user_id: int) -> list[dict[str, Any]]:
        """Linked reminders the device reports completed — rule 3's (and
        rule 4's deletion case's) input: candidates whose ledger task may
        not be done yet and needs completing."""
        from app.integrations.apple_reminders.models import Reminder

        rows = (
            session.query(Reminder)
            .filter(
                Reminder.user_id == user_id,
                Reminder.completed == True,  # noqa: E712
                Reminder.linked_task_uid.isnot(None),
            )
            .all()
        )
        return [{"uid": r.uid, "task_uid": r.linked_task_uid} for r in rows]

    def has_pending_complete(self, session: Session, *, user_id: int, reminder_uid: str) -> bool:
        """True if a `complete` command for this reminder is already queued
        and unresolved — the guard against dispatching a duplicate every
        tick while the device is offline or slow to confirm."""
        import json

        from app.integrations.apple_reminders.models import ReminderCommand

        rows = (
            session.query(ReminderCommand)
            .filter(
                ReminderCommand.user_id == user_id,
                ReminderCommand.action == "complete",
                ReminderCommand.status == "pending",
            )
            .all()
        )
        for row in rows:
            try:
                payload = json.loads(row.payload) if row.payload else {}
            except (TypeError, ValueError):
                continue
            if payload.get("args", {}).get("uid") == reminder_uid:
                return True
        return False

    def dispatch_complete(self, session: Session, *, user_id: int, user_name: str, reminder_uid: str) -> dict:
        """Queue an EventKit complete for `reminder_uid` via the existing
        server->daemon channel. Queued != applied — this never marks the
        local `Reminder.completed` itself; only the device's next push does
        (see `sync.py`)."""
        from app.integrations.apple_reminders.commands import dispatch_command

        return dispatch_command(
            session, user_id=user_id, user_name=user_name,
            action="complete", args={"uid": reminder_uid},
        )


FACADE = AppleRemindersFacade()
