"""Apple Reminders as an inlet — E chunk 6b (2026-09-04).

Retires `/reconcile-reminders`. See
`app/integrations/tasks/reminders_inlet.py`'s module docstring for the full
contract; this file pins the three directions, the deletion rule, and — the
two required loop-safety cases — that a task created from a reminder never
dispatches a reminder write back, and that a device-confirmed completion
this module itself caused can never re-complete its task.
"""

import json

import pytest

from app.integrations.apple_reminders import commands
from app.integrations.apple_reminders.models import Reminder, ReminderCommand
from app.integrations.tasks import reminders_inlet as inlet
from app.integrations.tasks.models import Task, TaskEvent

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch):
    """No SSE subscriber in the unit/db tier — `dispatch_command` takes its
    'no event loop bound' early return, which still writes the `pending`
    ReminderCommand row we assert on (queued != applied)."""
    monkeypatch.setattr(commands.stream_manager, "loop", None)


@pytest.fixture
def rendered_note(tmp_path, monkeypatch):
    """Point the ledger's render at a scratch file — `TASKS.create_from_capture`
    / `complete_task` both call `_render`, which writes `Task Backlog.md`."""
    note = tmp_path / "Task Backlog.md"
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    return note


def _reminder(session, *, user_id=1, uid="r-1", summary="Buy milk", completed=False,
              linked_task_uid=None, notes=None):
    r = Reminder(
        user_id=user_id, uid=uid, list_name="Reminders", summary=summary,
        notes=notes, priority=0, completed=completed, linked_task_uid=linked_task_uid,
    )
    session.add(r)
    session.commit()
    return r


def _task(session, *, uid="TASK-9001", status="inbox", owner_id=1, title="x"):
    t = Task(uid=uid, title=title, status=status, owner_id=owner_id, sort_order=1)
    session.add(t)
    session.commit()
    return t


# ─── rule 1: reminder → task ────────────────────────────────────────────────


class TestCapture:
    def test_open_unlinked_reminder_becomes_a_ledger_task(self, db_session, rendered_note):
        r = _reminder(db_session, summary="Buy milk", notes="oat, not cow")
        inlet.tick_once(db_session)
        db_session.refresh(r)

        assert r.linked_task_uid is not None
        task = db_session.query(Task).filter(Task.uid == r.linked_task_uid).one()
        assert task.status == "inbox"
        assert task.owner_id == 1
        assert task.source == "apple_reminders"
        assert task.title == "Buy milk"
        assert task.description == "oat, not cow"

    def test_already_linked_reminder_is_not_recaptured(self, db_session, rendered_note):
        task = _task(db_session, uid="TASK-1111")
        _reminder(db_session, uid="r-linked", linked_task_uid=task.uid)

        inlet.tick_once(db_session)
        inlet.tick_once(db_session)

        assert db_session.query(Task).filter(Task.uid == "TASK-1111").count() == 1

    def test_completed_unlinked_reminder_is_not_backfilled(self, db_session, rendered_note):
        """No manual reconciliation left means no backfill either — years of
        already-completed reminders must not flood the ledger with inbox
        tasks the moment this ships."""
        r = _reminder(db_session, uid="r-old-done", completed=True)
        inlet.tick_once(db_session)
        db_session.refresh(r)

        assert r.linked_task_uid is None
        assert db_session.query(Task).count() == 0

    def test_capture_is_per_user(self, db_session, rendered_note):
        _reminder(db_session, user_id=2, uid="r-sam", summary="Sam's item")
        inlet.tick_once(db_session)

        task = db_session.query(Task).one()
        assert task.owner_id == 2


# ─── rule 2: task → reminder ────────────────────────────────────────────────


class TestPushCompletion:
    @pytest.mark.parametrize("status", ["done", "dropped"])
    def test_closed_task_dispatches_a_reminder_complete_command(
        self, db_session, rendered_note, status,
    ):
        task = _task(db_session, uid="TASK-2222", status=status)
        r = _reminder(db_session, uid="r-2222", linked_task_uid=task.uid)

        inlet.tick_once(db_session)

        cmd = db_session.query(ReminderCommand).filter_by(action="complete").one()
        assert cmd.user_id == r.user_id
        assert json.loads(cmd.payload)["args"]["uid"] == "r-2222"
        assert cmd.status == "pending"

    def test_open_task_does_not_dispatch_anything(self, db_session, rendered_note):
        task = _task(db_session, uid="TASK-3333", status="next")
        _reminder(db_session, uid="r-3333", linked_task_uid=task.uid)

        inlet.tick_once(db_session)

        assert db_session.query(ReminderCommand).count() == 0

    def test_a_pending_complete_command_is_not_duplicated(self, db_session, rendered_note):
        task = _task(db_session, uid="TASK-4444", status="done")
        _reminder(db_session, uid="r-4444", linked_task_uid=task.uid)

        inlet.tick_once(db_session)
        inlet.tick_once(db_session)

        assert db_session.query(ReminderCommand).filter_by(action="complete").count() == 1


# ─── rule 3 (+ rule 4's deletion case): reminder → task ─────────────────────


class TestCompleteFromDevice:
    def test_device_confirmed_completion_completes_the_linked_task(
        self, db_session, rendered_note,
    ):
        task = _task(db_session, uid="TASK-5555", status="next")
        _reminder(db_session, uid="r-5555", linked_task_uid=task.uid, completed=True)

        inlet.tick_once(db_session)
        db_session.refresh(task)

        assert task.status == "done"

    def test_deletion_on_device_completes_rather_than_drops_the_task(
        self, db_session, rendered_note,
    ):
        """`sync_from_push`'s own stale-reaper marks a vanished reminder
        `completed` — from here that's indistinguishable from an ordinary
        completion, so the deletion rule is: complete, never drop."""
        task = _task(db_session, uid="TASK-6666", status="waiting")
        _reminder(db_session, uid="r-deleted", linked_task_uid=task.uid, completed=True)

        inlet.tick_once(db_session)
        db_session.refresh(task)

        assert task.status == "done"
        assert task.status != "dropped"


# ─── loop safety ─────────────────────────────────────────────────────────────


class TestLoopSafety:
    def test_capture_does_not_dispatch_a_reminder_write(
        self, db_session, rendered_note, monkeypatch,
    ):
        """Loop case 1: a task created from a reminder must never enqueue a
        reminder write back — otherwise every capture would immediately try
        to (re-)complete or re-add the very reminder it came from."""

        def _must_not_be_called(*a, **k):
            raise AssertionError("capture must never dispatch a reminder command")

        monkeypatch.setattr(commands, "dispatch_command", _must_not_be_called)

        r = _reminder(db_session, uid="r-loop-1", summary="No echo")
        inlet.tick_once(db_session)
        db_session.refresh(r)

        assert r.linked_task_uid is not None
        assert db_session.query(ReminderCommand).count() == 0

    def test_device_confirmed_completion_does_not_recomplete_an_already_done_task(
        self, db_session, rendered_note,
    ):
        """Loop case 2: rule 2 dispatches a complete command for a done task;
        once the device confirms (Reminder.completed becomes True on the same
        linked row), rule 3 must find nothing left to do — not re-run
        `_set_status`, not write a second `TaskEvent`, not mint a routine
        round a second time."""
        task = _task(db_session, uid="TASK-7777", status="done")
        _reminder(db_session, uid="r-7777", linked_task_uid=task.uid, completed=True)

        before = db_session.query(TaskEvent).filter_by(task_id=task.id).count()
        inlet.tick_once(db_session)
        db_session.refresh(task)
        after = db_session.query(TaskEvent).filter_by(task_id=task.id).count()

        assert task.status == "done"
        assert after == before


# ─── scheduler context: no bound user ───────────────────────────────────────


class TestSchedulerRender:
    """The tick runs from apscheduler, where nothing has called `use_user`.
    The first production tick (2026-09-04 10:33) captured ten reminders and
    then raised `current_user_id() called outside use_user()` inside the
    render. These pin the fix: render binds each vault's user explicitly."""

    def test_tick_renders_with_no_user_bound(self, db_session, monkeypatch, tmp_path):
        """Deliberately NOT the `rendered_note` fixture: that replaces
        `vault_paths.resolve` wholesale, which is exactly the lookup that
        raises when no user is bound — so under it the bug is invisible
        (the first version of this test passed with the bug reinstated).
        Redirect only the vaults root and let the real resolver run."""
        from app.auth import context as ctx
        from app.integrations.tasks.reminders_inlet import tick_once
        from app.models.users import User
        from app.services import vault_paths

        monkeypatch.setattr(vault_paths, "_vaults_root", lambda: tmp_path)
        users = db_session.query(User).filter_by(is_active=True).all()
        for u in users:
            (tmp_path / u.name).mkdir()

        _reminder(db_session, uid="r-sched")
        token = ctx._current_user_id.set(None)
        try:
            with pytest.raises(RuntimeError):
                ctx.current_user_id()  # genuinely unbound, as under apscheduler
            result = tick_once(db_session)
        finally:
            ctx._current_user_id.reset(token)

        assert len(result["captured"]) == 1
        written = [u.name for u in users if (tmp_path / u.name / "Task Backlog.md").exists()]
        assert written, "the tick must still render when unbound"

    def test_render_all_vaults_binds_each_user_explicitly(self, db_session, monkeypatch, tmp_path):
        from app.auth import context as ctx
        from app.integrations.tasks import tools

        seen: list[int | None] = []

        def _fake_write(session, user_id=None):
            seen.append(user_id)
            return "Task Backlog.md"

        monkeypatch.setattr(tools, "write_backlog_note", _fake_write)
        monkeypatch.setattr(
            "app.services.vault_paths.resolve",
            lambda path, user_id_override=None: tmp_path / "Task Backlog.md",
        )
        token = ctx._current_user_id.set(None)
        try:
            rendered = tools.render_all_vaults(db_session)
        finally:
            ctx._current_user_id.reset(token)

        assert seen and None not in seen, f"every render must carry an explicit user_id, got {seen}"
        assert len(rendered) == len(seen)

    def test_one_drifted_vault_does_not_sink_the_tick(self, db_session, monkeypatch, tmp_path):
        from app.integrations.tasks import tools
        from app.models.users import User

        users = db_session.query(User).filter_by(is_active=True).order_by(User.id).all()
        assert len(users) >= 1
        first = users[0].id

        def _fake_write(session, user_id=None):
            if user_id == first:
                raise RuntimeError("Task Backlog.md has been edited since it was last rendered.")
            return "Task Backlog.md"

        monkeypatch.setattr(tools, "write_backlog_note", _fake_write)
        monkeypatch.setattr(
            "app.services.vault_paths.resolve",
            lambda path, user_id_override=None: tmp_path / "Task Backlog.md",
        )
        rendered = tools.render_all_vaults(db_session)  # must not raise
        assert users[0].name not in rendered
