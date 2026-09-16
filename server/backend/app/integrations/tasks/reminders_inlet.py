"""Apple Reminders as an inlet — retires `/reconcile-reminders` (E chunk 6b,
2026-09-04).

Alex, 2026-09-03: *"the fact [/reconcile-reminders] exists suggests a wider
alignment issue."* Apple Reminders and the task ledger were two task stores
kept in line by hand. This module is the resolution: Reminders becomes an
**inlet only**.

## The contract

1. **Reminder -> task.** A reminder that is open and not yet linked to a
   ledger task becomes one: status `inbox`, owner = the reminder's own
   `user_id`, title/notes/due copied. The link — `Reminder.linked_task_uid`
   — is written through `apple_reminders`' facade (`link_task`) and is set
   once, forever; see that column's comment in
   `apple_reminders/models.py`.
2. **Task -> reminder.** A task that has gone `done` or `dropped` in the
   ledger, whose linked reminder is still open, gets a `complete` command
   dispatched down the existing queued write channel
   (`apple_reminders.facade.dispatch_complete` ->
   `commands.dispatch_command` — **queued != applied**; only the device's
   next push confirms it, per that package's own doctrine. This module
   never marks a `Reminder` row itself).
3. **Reminder -> task (again).** A reminder the device reports completed,
   whose linked task is not yet done, completes that task.
4. **Deletions.** `apple_reminders/sync.py::sync_from_push` already marks a
   reminder that disappeared from the device's snapshot as `completed` (its
   own stale-reaper, unchanged by this module) — from here that is
   indistinguishable from an ordinary completion, so rule 3 applies: **a
   reminder deleted on the device does NOT drop its linked task**, it
   completes it, same as any other completion. Symmetrically, **a task
   dropped in the ledger does NOT delete its reminder** — rule 2 treats
   `dropped` the same as `done` and only ever *completes* the reminder,
   matching `apple_reminders`' long-standing rule (`commands.py`) that a
   reminder is completed, never deleted, so the iCloud Recently Deleted
   view keeps everything recoverable.

## Why this reads and writes Apple Reminders through a facade

`tasks` depends on `apple_reminders` (`reminders.query` / `reminders.write`)
rather than the other way around. `apple_reminders` already
`provides=["reminders.query"]`, consumed by `system` (`system.alerts`),
consumed by `notifications` (`notify.push`), which `tasks` already
depended on for `tasks_nudge`/`tasks_transfer`. Had `apple_reminders`
instead declared a dependency back onto `tasks`, that chain closes into a
cycle boot validation rejects (`apple_reminders -> tasks -> notifications
-> system -> apple_reminders`) — the same "the package that pushes has to
be the one that pulls" shape as `inbox` pulling WhatsApp notes rather than
`whatsapp` pushing to it. See `manifest.py`'s `depends_on` comment.

## Why this can't loop

- **Task created here never dispatches a reminder write.** Rule 1 only ever
  calls ledger-local code (`Task`/`TaskEvent`, this package's own models) —
  it never touches `apple_reminders.facade.dispatch_complete`. See
  `tests/test_reminders_inlet.py::TestLoopSafety::test_capture_does_not_dispatch_a_reminder_write`.
- **A reminder completion this module caused can't re-complete its task.**
  Rule 2's dispatch eventually shows up, once the device confirms, as
  `Reminder.completed = True` on the very row `linked_task_uid` still points
  at — which rule 3 would otherwise try to complete again. Both rules read
  the linked task's *current* status before acting, and completing an
  already-done task is a no-op (`tools.py::_set_status`'s own guard: it
  returns immediately when the new status matches the current one) — so the
  second pass through either rule finds nothing left to do. See
  `tests/test_reminders_inlet.py::TestLoopSafety::test_device_confirmed_completion_does_not_recomplete_an_already_done_task`.
- **Rule 2 doesn't spam the device.** Before dispatching, it checks for an
  outstanding pending `complete` command for the same reminder uid
  (`facade.has_pending_complete`) and skips if one exists — otherwise every
  15-minute tick between "task went done" and "the daemon's next push
  confirms it" would queue another duplicate command.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.integrations.apple_reminders.facade import FACADE as REMINDERS
from app.integrations.tasks import dupes, routines
from app.integrations.tasks.models import Task, TaskEvent
from app.integrations.tasks.tools import _next_uid, _render, _set_status

logger = logging.getLogger(__name__)

# Loops end here — a task in either of these ledger statuses should
# complete its linked reminder, never re-trigger anything.
_COMPLETING_STATUSES = ("done", "dropped")


def _resolve_user_name(session: Session, user_id: int) -> str | None:
    from app.models.users import User

    user = session.get(User, user_id)
    return user.name if user else None


def _statuses_for(session: Session, uids: list[str]) -> dict[str, str]:
    if not uids:
        return {}
    rows = session.query(Task.uid, Task.status).filter(Task.uid.in_(uids)).all()
    return dict(rows)


def _capture_new(session: Session, *, user_id: int) -> list[str]:
    """Rule 1: open, unlinked reminders become ledger tasks."""
    captured: list[str] = []
    for item in REMINDERS.open_unlinked(session, user_id):
        task = Task(
            uid=_next_uid(session),
            title=(item.get("summary") or "")[:300],
            description=item.get("notes"),
            status="inbox",
            owner_id=user_id,
            due_at=item.get("due_date"),
            source="apple_reminders",
            # lios#224: a reminder is real data the person already entered on
            # their device, not an unreviewed suggestion — confirmed
            # immediately, same as any other non-extraction writer.
            confirmed_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            sort_order=(session.query(Task).count() + 1) * 1000,
        )
        session.add(task)
        session.flush()
        session.add(TaskEvent(
            task_id=task.id, from_status=None, to_status=task.status,
            actor_id=user_id, note="captured from apple_reminders",
        ))
        dupes.enqueue(session, task)
        session.commit()
        REMINDERS.link_task(session, reminder_uid=item["uid"], user_id=user_id, task_uid=task.uid)
        captured.append(task.uid)
    return captured


def _push_completions(session: Session, *, user_id: int) -> list[str]:
    """Rule 2: linked tasks that are done/dropped, whose reminder is still
    open, get a queued `complete` command."""
    linked = REMINDERS.linked_open(session, user_id)
    if not linked:
        return []

    statuses = _statuses_for(session, [item["task_uid"] for item in linked])
    user_name = _resolve_user_name(session, user_id)
    if user_name is None:
        return []

    dispatched: list[str] = []
    for item in linked:
        status = statuses.get(item["task_uid"])
        if status not in _COMPLETING_STATUSES:
            continue
        if REMINDERS.has_pending_complete(session, user_id=user_id, reminder_uid=item["uid"]):
            continue
        REMINDERS.dispatch_complete(
            session, user_id=user_id, user_name=user_name, reminder_uid=item["uid"],
        )
        dispatched.append(item["uid"])
    return dispatched


def _complete_from_device(session: Session, *, user_id: int) -> list[str]:
    """Rule 3 (and rule 4's deletion case, which arrives here indistinguishably
    from an ordinary completion): a completed, linked reminder completes its
    task, unless the task is already done/dropped or no longer exists."""
    linked = REMINDERS.linked_completed(session, user_id)
    if not linked:
        return []

    statuses = _statuses_for(session, [item["task_uid"] for item in linked])

    completed: list[str] = []
    for item in linked:
        status = statuses.get(item["task_uid"])
        if status in _COMPLETING_STATUSES or status is None:
            continue  # already closed, or the task no longer exists
        task = session.query(Task).filter(Task.uid == item["task_uid"]).one_or_none()
        if task is None:
            continue
        from app.auth.context import use_user

        with use_user(user_id):
            _set_status(session, task, "done", note="completed via Apple Reminders")
        dupes.enqueue(session, task)
        routines.mint_next_on_complete(session, task)
        session.commit()
        completed.append(item["task_uid"])
    return completed


def tick_once(session: Session) -> dict:
    """Run all three rules for every active user. Order matters: capture
    before push/complete, so a reminder captured this tick is immediately
    eligible for the other two directions on the very next tick rather than
    waiting a full cycle for no reason — and push before complete, so a task
    someone finished in the same window as its reminder being ticked off on
    the phone converges on "reminder completed" either way."""
    from app.models.users import User

    result: dict[str, list[str]] = {"captured": [], "pushed": [], "completed": []}
    users = session.query(User).filter_by(is_active=True).order_by(User.id).all()
    for user in users:
        result["captured"] += _capture_new(session, user_id=user.id)
        result["pushed"] += _push_completions(session, user_id=user.id)
        result["completed"] += _complete_from_device(session, user_id=user.id)

    if result["captured"] or result["completed"]:
        # No request here, so no bound user: `_render` would raise (it did,
        # on the first tick after deploy). Bind per vault instead.
        from app.integrations.tasks.tools import render_all_vaults  # noqa: PLC0415

        render_all_vaults(session)
    return result


async def run_tick() -> None:
    """Cron entry point (see `manifest.py::background_tasks`), every 15
    minutes — same cadence and shape as `tasks.routines.run_tick`. Writes
    SyncState under `apple_reminders` (a distinct row from `tasks`' own
    `tasks_routines_tick`) so a broken inlet shows up on the dashboard
    rather than silently starving the ledger of new captures."""
    import asyncio

    from app.db import get_db
    from app.scheduler import _update_sync_state

    def _run() -> dict:
        db = get_db()
        with db.session() as session:
            return tick_once(session)

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        logger.exception("reminders inlet tick failed")
        _update_sync_state(
            "apple_reminders", status="error", error=str(exc)[:200],
            trigger="reminders_inlet_tick",
        )
        return

    if result["captured"] or result["pushed"] or result["completed"]:
        logger.info("reminders inlet tick: %s", result)
    _update_sync_state("apple_reminders", status="ok", trigger="reminders_inlet_tick")
