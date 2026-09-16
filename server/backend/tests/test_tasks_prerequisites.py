"""C2 "runs with prerequisites" (lios#156, proposal agreed 2026-09-11).

`tasks.requires_task_id` — one nullable, single-edge FK, declared on the
round/task INSTANCE, not the routine template. Covers: the model/migration
round trip, the computed `prerequisite: {"text", "satisfied"} | null` field
on tool payloads (never a raw id in that sub-object), the satisfied logic
across done/dropped/open, scoping refusal for a prerequisite the caller
cannot see, and `routines_update`'s current-round-only semantics.
"""

import json

import pytest

pytestmark = pytest.mark.db


def _call(handler, session, **args):
    return json.loads(handler(session, args))


@pytest.fixture
def backlog_note(tmp_path, monkeypatch):
    note = tmp_path / "Task Backlog.md"
    note.write_text("placeholder\n")
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    return note


# ── model / migration round trip ────────────────────────────────────────────


@pytest.mark.anyio
async def test_requires_task_id_round_trips_through_the_column(db_session, backlog_note):
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_add_handler

    prereq = _call(tasks_add_handler, db_session, title="Bring the bins in")["created"]["uid"]
    dependent = _call(
        tasks_add_handler, db_session, title="Put the bins out", requires_task=prereq,
    )["created"]["uid"]

    row = db_session.query(Task).filter(Task.uid == dependent).one()
    assert row.requires_task_id == db_session.query(Task).filter(Task.uid == prereq).one().id


# ── computed prerequisite field ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_tasks_add_computes_prerequisite_sentence(db_session, backlog_note):
    from app.integrations.tasks.tools import tasks_add_handler

    prereq = _call(tasks_add_handler, db_session, title="Bring the bins in")["created"]["uid"]
    out = _call(
        tasks_add_handler, db_session, title="Put the bins out", requires_task=prereq,
    )["created"]

    assert out["prerequisite"] == {"text": "Bring the bins in", "satisfied": False}
    # The raw id is fine alongside the computed field in the TOOL payload.
    assert out["requires_task_id"] is not None


@pytest.mark.anyio
async def test_no_prerequisite_is_null_not_a_missing_key(db_session, backlog_note):
    from app.integrations.tasks.tools import tasks_add_handler

    out = _call(tasks_add_handler, db_session, title="Ordinary task")["created"]
    assert out["prerequisite"] is None
    assert out["requires_task_id"] is None


@pytest.mark.anyio
async def test_tasks_query_computes_prerequisite_for_every_row(db_session, backlog_note):
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    prereq = _call(tasks_add_handler, db_session, title="Bring the bins in")["created"]["uid"]
    dependent = _call(
        tasks_add_handler, db_session, title="Put the bins out", requires_task=prereq,
    )["created"]["uid"]

    rows = {t["uid"]: t for t in _call(tasks_query_handler, db_session, limit=500)["tasks"]}
    assert rows[dependent]["prerequisite"] == {"text": "Bring the bins in", "satisfied": False}
    assert rows[prereq]["prerequisite"] is None


# ── satisfied logic: done / dropped / open ──────────────────────────────────


@pytest.mark.anyio
async def test_satisfied_is_false_while_prerequisite_is_open(db_session, backlog_note):
    from app.integrations.tasks.tools import tasks_add_handler

    prereq = _call(tasks_add_handler, db_session, title="Bring the bins in")["created"]["uid"]
    dependent = _call(
        tasks_add_handler, db_session, title="Put the bins out", requires_task=prereq,
    )["created"]
    assert dependent["prerequisite"]["satisfied"] is False


@pytest.mark.anyio
async def test_satisfied_becomes_true_once_prerequisite_is_done(db_session, backlog_note):
    from app.integrations.tasks.tools import (
        tasks_add_handler, tasks_complete_handler, tasks_query_handler,
    )

    prereq = _call(tasks_add_handler, db_session, title="Bring the bins in")["created"]["uid"]
    dependent = _call(
        tasks_add_handler, db_session, title="Put the bins out", requires_task=prereq,
    )["created"]["uid"]

    _call(tasks_complete_handler, db_session, uid=prereq)

    rows = {t["uid"]: t for t in _call(tasks_query_handler, db_session, limit=500)["tasks"]}
    assert rows[dependent]["prerequisite"]["satisfied"] is True


@pytest.mark.anyio
async def test_satisfied_becomes_true_when_prerequisite_is_dropped(db_session, backlog_note):
    """A skip is a signal, not a stuck state — a dropped (skipped/declined)
    prerequisite unblocks the dependent exactly like a done one, per the
    proposal's (c) semantics."""
    from app.integrations.tasks.tools import (
        tasks_add_handler, tasks_query_handler, tasks_update_handler,
    )

    prereq = _call(tasks_add_handler, db_session, title="Bring the bins in")["created"]["uid"]
    dependent = _call(
        tasks_add_handler, db_session, title="Put the bins out", requires_task=prereq,
    )["created"]["uid"]

    _call(tasks_update_handler, db_session, uid=prereq, status="dropped")

    rows = {t["uid"]: t for t in _call(tasks_query_handler, db_session, limit=500)["tasks"]}
    assert rows[dependent]["prerequisite"]["satisfied"] is True


@pytest.mark.anyio
async def test_a_routine_skip_satisfies_a_dependent_prerequisite(db_session, backlog_note):
    """The other half of the same rule, exercised through an actual routine
    skip (`skip_round`), not a plain status update."""
    from app.integrations.tasks.routines import routines_add_handler, routines_skip_handler
    from app.integrations.tasks.tools import (
        tasks_add_handler, tasks_query_handler, tasks_update_handler,
    )

    added = _call(
        routines_add_handler, db_session, title="Bring the bins in",
        done_when="Bins are inside", schedule_kind="interval", schedule_spec="P7D",
    )
    round_uid = added["first_round"]
    dependent = _call(
        tasks_add_handler, db_session, title="Put the bins out", requires_task=round_uid,
    )["created"]["uid"]

    _call(routines_skip_handler, db_session, routine=added["created"]["uid"])

    rows = {t["uid"]: t for t in _call(tasks_query_handler, db_session, limit=500)["tasks"]}
    assert rows[dependent]["prerequisite"]["satisfied"] is True


# ── mutation check ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_mutation_only_done_would_wrongly_leave_a_dropped_prerequisite_stuck(
    db_session, backlog_note,
):
    """Pins the doctrine directly against the constant: if a future edit
    narrows `PREREQUISITE_SATISFIED_STATUSES` back to `("done",)` — the bug
    this feature explicitly rejects — this test must fail."""
    from app.integrations.tasks.models import PREREQUISITE_SATISFIED_STATUSES
    assert "dropped" in PREREQUISITE_SATISFIED_STATUSES
    assert "done" in PREREQUISITE_SATISFIED_STATUSES


# ── scoping: a prerequisite must not leak an invisible task ─────────────────


@pytest.mark.anyio
async def test_cannot_reference_another_users_task_as_a_prerequisite(db_session, backlog_note):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_add_handler

    with use_user(2):
        sams_task = _call(tasks_add_handler, db_session, title="Sam's private thing")
        sams_uid = sams_task["created"]["uid"]

    # Alex (user 1, the default in these tests) cannot see it.
    with pytest.raises(ValueError, match="Unknown task uid"):
        _call(tasks_add_handler, db_session, title="Alex's task", requires_task=sams_uid)


@pytest.mark.anyio
async def test_can_reference_an_unowned_task_as_a_prerequisite(db_session, backlog_note):
    """Household-shared, unowned rows (e.g. imported) are visible to anyone —
    only a task owned by a DIFFERENT user is refused."""
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_add_handler

    unowned = Task(uid="TASK-9001", title="Unowned import", status="next")
    db_session.add(unowned)
    db_session.commit()

    out = _call(
        tasks_add_handler, db_session, title="Depends on unowned", requires_task="TASK-9001",
    )["created"]
    assert out["prerequisite"]["text"] == "Unowned import"


@pytest.mark.anyio
async def test_unknown_prerequisite_uid_is_refused(db_session, backlog_note):
    from app.integrations.tasks.tools import tasks_add_handler

    with pytest.raises(ValueError, match="Unknown task uid"):
        _call(tasks_add_handler, db_session, title="X", requires_task="TASK-9999")


@pytest.mark.anyio
async def test_a_task_cannot_require_itself(db_session, backlog_note):
    from app.integrations.tasks.tools import tasks_add_handler, tasks_update_handler

    uid = _call(tasks_add_handler, db_session, title="Self-referential")["created"]["uid"]
    with pytest.raises(ValueError, match="cannot require itself"):
        _call(tasks_update_handler, db_session, uid=uid, requires_task=uid)


# ── tasks_update: set and clear ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_tasks_update_sets_and_clears_the_prerequisite(db_session, backlog_note):
    from app.integrations.tasks.tools import tasks_add_handler, tasks_update_handler

    prereq = _call(tasks_add_handler, db_session, title="Bring the bins in")["created"]["uid"]
    dependent = _call(tasks_add_handler, db_session, title="Put the bins out")["created"]["uid"]

    out = _call(tasks_update_handler, db_session, uid=dependent, requires_task=prereq)["updated"]
    assert out["prerequisite"]["text"] == "Bring the bins in"

    cleared = _call(tasks_update_handler, db_session, uid=dependent, requires_task=None)["updated"]
    assert cleared["prerequisite"] is None
    assert cleared["requires_task_id"] is None


# ── routines_add / routines_update ──────────────────────────────────────────


@pytest.mark.anyio
async def test_routines_add_sets_the_prerequisite_on_the_first_round(db_session, backlog_note):
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.routines import routines_add_handler
    from app.integrations.tasks.tools import tasks_add_handler

    prereq = _call(tasks_add_handler, db_session, title="Kit washed")["created"]["uid"]
    added = _call(
        routines_add_handler, db_session, title="Pack hockey bag", done_when="Bag packed",
        schedule_kind="fixed", schedule_spec="FREQ=WEEKLY;BYDAY=WE;BYHOUR=18",
        requires_task=prereq,
    )
    round_task = db_session.query(Task).filter(Task.uid == added["first_round"]).one()
    assert round_task.requires_task_id == db_session.query(Task).filter(Task.uid == prereq).one().id


@pytest.mark.anyio
async def test_routines_update_sets_prerequisite_on_current_round_only(db_session, backlog_note):
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.routines import routines_add_handler, routines_update_handler
    from app.integrations.tasks.tools import tasks_add_handler

    added = _call(
        routines_add_handler, db_session, title="Pack hockey bag", done_when="Bag packed",
        schedule_kind="fixed", schedule_spec="FREQ=WEEKLY;BYDAY=WE;BYHOUR=18",
    )
    routine_uid = added["created"]["uid"]
    first_round = added["first_round"]

    prereq = _call(tasks_add_handler, db_session, title="Kit washed")["created"]["uid"]
    out = _call(routines_update_handler, db_session, routine=routine_uid, requires_task=prereq)
    assert out["updated"]["current_round"]["prerequisite"]["text"] == "Kit washed"

    round_task = db_session.query(Task).filter(Task.uid == first_round).one()
    assert round_task.requires_task_id == db_session.query(Task).filter(Task.uid == prereq).one().id

    # Clearing works too.
    cleared = _call(routines_update_handler, db_session, routine=routine_uid, requires_task=None)
    assert cleared["updated"]["current_round"]["prerequisite"] is None


@pytest.mark.anyio
async def test_routines_update_refuses_a_prerequisite_with_no_open_round(db_session, backlog_note):
    from app.integrations.tasks.routines import routines_add_handler, routines_update_handler
    from app.integrations.tasks.tools import tasks_add_handler

    added = _call(
        routines_add_handler, db_session, title="Pack hockey bag", done_when="Bag packed",
        schedule_kind="fixed", schedule_spec="FREQ=WEEKLY;BYDAY=WE;BYHOUR=18",
    )
    # Deactivating drops the current open round without minting another.
    _call(routines_update_handler, db_session, routine=added["created"]["uid"], active=False)

    prereq = _call(tasks_add_handler, db_session, title="Kit washed")["created"]["uid"]
    with pytest.raises(ValueError, match="no open round"):
        _call(
            routines_update_handler, db_session, routine=added["created"]["uid"],
            requires_task=prereq,
        )
