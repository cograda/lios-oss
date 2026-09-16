"""Absence detection (R5, Wave 2, 2026-09-04) — alert on silence, once.

Three checks in `app/integrations/tasks/absence.py`:
  - a routine whose window closed with no round completed
  - a `waiting` task past due with no note since
  - a snag unanswered for weeks

The binding contract, per the backlog item this closes: "a fixture routine
with a missed window raises exactly one alert, once." Each test below pins
one check's create -> (repeat, no duplicate) -> resolve lifecycle.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


def _call(handler, session, **args):
    return json.loads(handler(session, args))


@pytest.fixture
def backlog_note(tmp_path, monkeypatch):
    """Route the rendered backlog to a temp file — several handlers exercised
    here (`routines_add`, `tasks_complete`) re-render on write. Same fixture
    shape as test_tasks_routines.py."""
    note = tmp_path / "Task Backlog.md"
    note.write_text("placeholder\n")
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    return note


# ── routine windows ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_missed_routine_window_alerts_exactly_once_and_clears_on_completion(
    db_session_concurrent, backlog_note,
):
    """Uses `db_session_concurrent`, not `db_session` — this is the one test
    in this file whose finding ordering depends on `TaskEvent.at`'s
    `server_default=func.now()` genuinely advancing between two events
    written in the same test (the routine's skip, then its later
    completion). Postgres's `now()` is *transaction-start* time, frozen for
    the life of one transaction — `real_db`'s whole-test SAVEPOINT wraps the
    entire test in exactly one, so both events would get the identical
    timestamp and the query that orders by `.at.desc()` to find the latest
    close event could return either one. `real_db_concurrent` gives each
    write its own genuinely-committing session/transaction, so `now()`
    actually advances between them, matching production (each handler call
    there runs in its own transaction too). Measured, not theorised: this
    test failed 3/3 runs under `db_session` and passed 3/3 under this one.
    """
    db_session = db_session_concurrent
    from app.integrations.tasks import absence
    from app.integrations.tasks.models import AbsenceAlert, Task
    from app.integrations.tasks.routines import routines_add_handler, tick_once
    from app.integrations.tasks.tools import tasks_complete_handler

    added = _call(
        routines_add_handler, db_session, title="Take out bins", done_when="Bins at curb",
        schedule_kind="interval", schedule_spec="P7D",
    )
    first_round = db_session.query(Task).filter(Task.uid == added["first_round"]).one()
    # Backdate as if minted 8 days ago — past the 7-day interval, so the next
    # tick finds it overdue and skips it (see test_tasks_routines.py's
    # identical setup for the underlying routines.py behaviour).
    first_round.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    db_session.commit()

    now = datetime.now(timezone.utc)

    # Tick 1: routines.tick_once() skips the stale round and mints the next;
    # absence.reconcile_alerts (run_tick's own second step) then sees that
    # skip and raises exactly one alert.
    tick_once(db_session)
    result1 = absence.reconcile_alerts(db_session, now)
    assert len(result1["created"]) == 1

    open_rows = (
        db_session.query(AbsenceAlert)
        .filter(AbsenceAlert.kind == "routine_window", AbsenceAlert.resolved_at.is_(None))
        .all()
    )
    assert len(open_rows) == 1
    assert open_rows[0].ref == added["created"]["uid"]

    # Tick 2: nothing else has happened — the routine's last close event is
    # still the same skip, so this must NOT re-alert (the whole point of the
    # dedup key including the skip's own timestamp).
    result2 = absence.reconcile_alerts(db_session, now + timedelta(minutes=15))
    assert result2["created"] == []
    assert result2["resolved"] == []
    still_open = (
        db_session.query(AbsenceAlert)
        .filter(AbsenceAlert.kind == "routine_window", AbsenceAlert.resolved_at.is_(None))
        .count()
    )
    assert still_open == 1
    total_rows = db_session.query(AbsenceAlert).filter(AbsenceAlert.kind == "routine_window").count()
    assert total_rows == 1  # exactly one alert row, ever, for this incident

    assert len(absence.open_alerts_for(db_session, owner_id=None)) == 1

    # Completing the round the skip minted supersedes the skip as the
    # routine's last close event, so the finding disappears and the open
    # alert row resolves.
    current_round = db_session.query(Task).filter(
        Task.routine_id == first_round.routine_id, Task.status == "next",
    ).one()
    _call(tasks_complete_handler, db_session, uid=current_round.uid)

    result3 = absence.reconcile_alerts(db_session, now + timedelta(minutes=30))
    assert result3["resolved"] == [f"absence:routine_window:{added['created']['uid']}:{open_rows[0].since.isoformat()}"]

    db_session.expire_all()
    still_open_after = (
        db_session.query(AbsenceAlert)
        .filter(AbsenceAlert.kind == "routine_window", AbsenceAlert.resolved_at.is_(None))
        .count()
    )
    assert still_open_after == 0
    assert absence.open_alerts_for(db_session, owner_id=None) == []


# ── waiting items ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_stale_waiting_alerts_once_and_a_note_clears_it(db_session, backlog_note):
    from app.integrations.tasks import absence
    from app.integrations.tasks.models import AbsenceAlert, Task, TaskComment
    from app.integrations.tasks.tools import tasks_note_add_handler

    now = datetime.now(timezone.utc)
    due = now - timedelta(days=10)  # past the default 3-day grace
    task = Task(
        uid="TASK-9001", title="Reorder prescription", status="waiting",
        owner_id=1, due_at=due, created_at=now - timedelta(days=20),
        confirmed_at=now - timedelta(days=20),
    )
    db_session.add(task)
    db_session.commit()

    result1 = absence.reconcile_alerts(db_session, now)
    assert len(result1["created"]) == 1

    open_row = (
        db_session.query(AbsenceAlert)
        .filter(AbsenceAlert.kind == "waiting", AbsenceAlert.ref == "TASK-9001")
        .one()
    )
    assert open_row.resolved_at is None

    # A second tick with nothing changed must not re-alert.
    result2 = absence.reconcile_alerts(db_session, now + timedelta(minutes=15))
    assert result2["created"] == []
    assert db_session.query(AbsenceAlert).filter(AbsenceAlert.ref == "TASK-9001").count() == 1

    # A note added now (after due_at) answers the silence.
    _call(tasks_note_add_handler, db_session, uid="TASK-9001", body="chased the pharmacy")
    assert db_session.query(TaskComment).filter(TaskComment.task_id == task.id).count() == 1

    result3 = absence.reconcile_alerts(db_session, now + timedelta(minutes=30))
    assert len(result3["resolved"]) == 1
    db_session.refresh(open_row)
    assert open_row.resolved_at is not None


# ── snags ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_unanswered_snag_alerts_once_and_a_status_change_clears_it(db_session):
    from app.integrations.snags.models import Snag
    from app.integrations.tasks import absence
    from app.integrations.tasks.models import AbsenceAlert

    now = datetime.now(timezone.utc)
    snag = Snag(
        uid="SNAG-9001", title="Cracked tile", room="Kitchen", trade="tiler",
        severity="minor", status="reported",
        reported_at=now - timedelta(weeks=4),  # past the default 3-week threshold
    )
    db_session.add(snag)
    db_session.commit()

    result1 = absence.reconcile_alerts(db_session, now)
    assert len(result1["created"]) == 1
    open_row = (
        db_session.query(AbsenceAlert)
        .filter(AbsenceAlert.kind == "snag", AbsenceAlert.ref == "SNAG-9001")
        .one()
    )
    assert open_row.owner_id is None  # household-shared, no single owner

    result2 = absence.reconcile_alerts(db_session, now + timedelta(minutes=15))
    assert result2["created"] == []
    assert db_session.query(AbsenceAlert).filter(AbsenceAlert.ref == "SNAG-9001").count() == 1

    # The trade responds — status moves past "reported".
    snag.status = "accepted"
    db_session.commit()

    result3 = absence.reconcile_alerts(db_session, now + timedelta(minutes=30))
    assert len(result3["resolved"]) == 1
    db_session.refresh(open_row)
    assert open_row.resolved_at is not None


# ── burst guard ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_burst_of_new_findings_sends_one_digest_not_one_push_each(
    db_session, monkeypatch,
):
    """Below `absence_burst_threshold` (default 5), each new finding still
    pushes individually. At or above it, none push individually — one
    combined digest push goes out instead. Rows are created either way; only
    the push behaviour branches. See absence.py's module docstring for why
    this guard exists (the 2026-09-04 unanswered_snags backfill, 172 pushes
    in one day for what was a one-time historical debt dump, not ongoing
    noise)."""
    from app.integrations.tasks import absence
    from app.integrations.tasks.models import AbsenceAlert, Task

    new_calls: list = []
    burst_calls: list = []
    monkeypatch.setattr(absence, "_notify_new", lambda f: new_calls.append(f))
    monkeypatch.setattr(absence, "_notify_burst", lambda fs: burst_calls.append(fs))

    now = datetime.now(timezone.utc)

    def make_waiting(n: int) -> None:
        for i in range(n):
            db_session.add(Task(
                uid=f"TASK-BURST-{i}", title=f"Overdue {i}", status="waiting",
                owner_id=1, due_at=now - timedelta(days=10),
                created_at=now - timedelta(days=20),
                confirmed_at=now - timedelta(days=20),
            ))
        db_session.commit()

    # Below threshold (default 5): individual pushes, no digest.
    make_waiting(3)
    result = absence.reconcile_alerts(db_session, now)
    assert len(result["created"]) == 3
    assert len(new_calls) == 3
    assert burst_calls == []

    new_calls.clear()
    db_session.query(AbsenceAlert).delete()
    db_session.query(Task).delete()
    db_session.commit()

    # At threshold: one digest, no individual pushes.
    make_waiting(5)
    result = absence.reconcile_alerts(db_session, now + timedelta(hours=1))
    assert len(result["created"]) == 5
    assert new_calls == []
    assert len(burst_calls) == 1
    assert len(burst_calls[0]) == 5


# ── the tool surface ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_tasks_absence_alerts_tool_scopes_by_owner(db_session):
    from app.auth.context import use_user
    from app.integrations.tasks import absence
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_absence_alerts_handler

    now = datetime.now(timezone.utc)
    mine = Task(
        uid="TASK-9101", title="Mine", status="waiting", owner_id=1,
        due_at=now - timedelta(days=10), created_at=now - timedelta(days=20),
        confirmed_at=now - timedelta(days=20),
    )
    theirs = Task(
        uid="TASK-9102", title="Sam's", status="waiting", owner_id=2,
        due_at=now - timedelta(days=10), created_at=now - timedelta(days=20),
        confirmed_at=now - timedelta(days=20),
    )
    db_session.add_all([mine, theirs])
    db_session.commit()

    absence.reconcile_alerts(db_session, now)

    with use_user(1):
        out = _call(tasks_absence_alerts_handler, db_session)
    refs = {a["ref"] for a in out["alerts"]}
    assert refs == {"TASK-9101"}  # not Sam's

    with use_user(1):
        out_household = _call(tasks_absence_alerts_handler, db_session, household=True)
    refs_household = {a["ref"] for a in out_household["alerts"]}
    assert refs_household == {"TASK-9101", "TASK-9102"}
