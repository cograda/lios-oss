"""Routines and rounds (chunk E3, 2026-09-03).

A routine is a recurring loop template; a round is one occurrence and IS a
`tasks` row (`routine_id` set). These tests exercise: minting, the
just-in-time invariant (never two open rounds), skip-then-mint, interval
re-mint on complete, routine-level transfer/accept moving the open round's
owner, negative cases, the renderer's Routines section, and the review/
duplicate-detection exclusions.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


def _call(handler, session, **args):
    return json.loads(handler(session, args))


@pytest.fixture
def backlog_note(tmp_path, monkeypatch):
    """Route the rendered backlog to a temp file, same as test_tasks_tools.py."""
    note = tmp_path / "Task Backlog.md"
    note.write_text("placeholder\n")
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    return note


# ── minting / just-in-time ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_add_mints_the_first_round_immediately(db_session, backlog_note):
    from app.integrations.tasks.routines import routines_add_handler
    from app.integrations.tasks.tools import tasks_query_handler

    out = _call(
        routines_add_handler, db_session, title="Take out bins", done_when="Bins are at the curb",
        schedule_kind="interval", schedule_spec="P7D",
    )
    assert out["created"]["uid"].startswith("RTN-")
    assert out["first_round"].startswith("TASK-")

    rows = _call(tasks_query_handler, db_session, routines="only")
    assert rows["count"] == 1
    assert rows["tasks"][0]["uid"] == out["first_round"]
    assert rows["tasks"][0]["routine_uid"] == out["created"]["uid"]
    assert rows["tasks"][0]["status"] == "next"


@pytest.mark.anyio
async def test_only_one_open_round_ever_exists(db_session, backlog_note):
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.routines import routines_add_handler

    out = _call(
        routines_add_handler, db_session, title="Water plants", done_when="Every plant watered",
        schedule_kind="interval", schedule_spec="P3D",
    )
    routine_uid = out["created"]["uid"]

    open_rounds = db_session.query(Task).filter(
        Task.routine_id == db_session.query(Task).filter(Task.uid == out["first_round"]).one().routine_id,
        Task.status.in_(("inbox", "next", "waiting", "scheduled")),
    ).count()
    assert open_rounds == 1


@pytest.mark.anyio
async def test_interval_routine_mints_next_round_on_complete(db_session, backlog_note):
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.routines import routines_add_handler
    from app.integrations.tasks.tools import tasks_complete_handler

    added = _call(
        routines_add_handler, db_session, title="Water plants", done_when="Every plant watered",
        schedule_kind="interval", schedule_spec="P3D",
    )
    first_round = added["first_round"]

    out = _call(tasks_complete_handler, db_session, uid=first_round)
    assert out["completed"]["status"] == "done"
    assert "next_round" in out
    assert out["next_round"] != first_round

    routine_id = db_session.query(Task).filter(Task.uid == first_round).one().routine_id
    open_rounds = db_session.query(Task).filter(
        Task.routine_id == routine_id, Task.status.in_(("inbox", "next", "waiting", "scheduled")),
    ).all()
    assert len(open_rounds) == 1
    assert open_rounds[0].uid == out["next_round"]


@pytest.mark.anyio
async def test_completing_an_ordinary_task_reports_no_next_round(db_session, backlog_note):
    from app.integrations.tasks.tools import tasks_add_handler, tasks_complete_handler

    added = _call(tasks_add_handler, db_session, title="One-off task")
    out = _call(tasks_complete_handler, db_session, uid=added["created"]["uid"])
    assert "next_round" not in out


# ── skip ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_tick_skips_a_stale_round_and_mints_the_next(db_session, backlog_note):
    from app.integrations.tasks.models import Task, TaskEvent
    from app.integrations.tasks.routines import routines_add_handler, tick_once

    added = _call(
        routines_add_handler, db_session, title="Take out bins", done_when="Bins at curb",
        schedule_kind="interval", schedule_spec="P7D",
    )
    first_round = db_session.query(Task).filter(Task.uid == added["first_round"]).one()
    # Backdate the round as if it was minted 8 days ago (past the 7-day interval).
    first_round.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    db_session.commit()

    result = tick_once(db_session)
    assert added["first_round"] in result["skipped"]
    assert len(result["minted"]) == 1

    db_session.refresh(first_round)
    assert first_round.status == "dropped"
    skip_event = (
        db_session.query(TaskEvent)
        .filter(TaskEvent.task_id == first_round.id, TaskEvent.field == "skip")
        .one()
    )
    assert skip_event.to_status == "dropped"

    open_rounds = db_session.query(Task).filter(
        Task.routine_id == first_round.routine_id,
        Task.status.in_(("inbox", "next", "waiting", "scheduled")),
    ).count()
    assert open_rounds == 1


@pytest.mark.anyio
async def test_tick_is_idempotent(db_session, backlog_note):
    from app.integrations.tasks.routines import routines_add_handler, tick_once

    _call(
        routines_add_handler, db_session, title="Daily check", done_when="Checked",
        schedule_kind="window", schedule_spec="bedtime",
    )
    first = tick_once(db_session)
    second = tick_once(db_session)
    assert second["minted"] == []
    assert second["skipped"] == []


@pytest.mark.anyio
async def test_routines_skip_closes_and_mints(db_session, backlog_note):
    from app.integrations.tasks.routines import routines_add_handler, routines_skip_handler

    added = _call(
        routines_add_handler, db_session, title="Take out bins", done_when="Bins at curb",
        schedule_kind="interval", schedule_spec="P7D",
    )
    out = _call(routines_skip_handler, db_session, routine=added["created"]["uid"], note="forgot")
    assert out["skipped"] == added["first_round"]
    assert out["minted"] != added["first_round"]


# ── transfer / accept / decline ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_routines_accept_moves_default_owner_and_current_round_owner(db_session, backlog_note):
    from app.auth.context import use_user
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.routines import (
        routines_accept_handler, routines_add_handler, routines_transfer_handler,
    )

    added = _call(
        routines_add_handler, db_session, title="Bins", done_when="Done",
        schedule_kind="interval", schedule_spec="P7D", default_owner="alex",
    )
    routine_uid = added["created"]["uid"]
    _call(routines_transfer_handler, db_session, routine=routine_uid, to_user="sam")

    with use_user(2):
        out = _call(routines_accept_handler, db_session, routine=routine_uid)

    assert out["accepted"]["default_owner_id"] == 2
    assert out["accepted"]["pending_owner_id"] is None

    round_task = db_session.query(Task).filter(Task.uid == added["first_round"]).one()
    assert round_task.owner_id == 2


@pytest.mark.anyio
async def test_only_the_pending_owner_can_accept_a_routine(db_session, backlog_note):
    from app.integrations.tasks.routines import (
        routines_accept_handler, routines_add_handler, routines_transfer_handler,
    )

    added = _call(
        routines_add_handler, db_session, title="Bins", done_when="Done",
        schedule_kind="interval", schedule_spec="P7D",
    )
    routine_uid = added["created"]["uid"]
    _call(routines_transfer_handler, db_session, routine=routine_uid, to_user="sam")

    with pytest.raises(ValueError, match="no transfer pending"):
        _call(routines_accept_handler, db_session, routine=routine_uid)


@pytest.mark.anyio
async def test_routine_row_says_who_handed_it_over(db_session, backlog_note):
    """Same receiving-side fix as tasks: the routine row carries the
    requester as `pending_from_id` while the hand-over is outstanding, and
    nothing once it is answered."""
    from app.auth.context import use_user
    from app.integrations.tasks.routines import (
        routines_accept_handler, routines_add_handler, routines_list_handler,
        routines_transfer_handler,
    )

    added = _call(
        routines_add_handler, db_session, title="Bins", done_when="Done",
        schedule_kind="interval", schedule_spec="P7D", default_owner="alex",
    )
    routine_uid = added["created"]["uid"]
    assert added["created"]["pending_from_id"] is None

    out = _call(routines_transfer_handler, db_session, routine=routine_uid, to_user="sam")
    assert out["transferred"]["pending_from_id"] == 1
    with use_user(2):
        listed = _call(routines_list_handler, db_session)
        assert [r["pending_from_id"] for r in listed["routines"] if r["uid"] == routine_uid] == [1]
        accepted = _call(routines_accept_handler, db_session, routine=routine_uid)
    assert accepted["accepted"]["pending_from_id"] is None


@pytest.mark.anyio
async def test_routine_transfer_events_record_who_acted(db_session, backlog_note):
    """Routine-level transfer events were written with no `actor_id` until
    2026-09-06, so the ledger knew a hand-over was requested but not by whom.
    Every step now names its actor, the same as task transfer events do."""
    from app.auth.context import use_user
    from app.integrations.tasks.models import TaskEvent
    from app.integrations.tasks.routines import (
        routines_accept_handler, routines_add_handler, routines_transfer_handler,
    )

    added = _call(
        routines_add_handler, db_session, title="Bins", done_when="Done",
        schedule_kind="interval", schedule_spec="P7D", default_owner="alex",
    )
    routine_uid = added["created"]["uid"]
    _call(routines_transfer_handler, db_session, routine=routine_uid, to_user="sam")
    with use_user(2):
        _call(routines_accept_handler, db_session, routine=routine_uid)

    events = (
        db_session.query(TaskEvent.note, TaskEvent.actor_id)
        .filter(TaskEvent.field == "transfer", TaskEvent.task_id.is_(None))
        .order_by(TaskEvent.id)
        .all()
    )
    assert [tuple(e) for e in events] == [("requested", 1), ("accepted", 2)]


@pytest.mark.anyio
async def test_routines_decline_clears_pending_without_moving_owner(db_session, backlog_note):
    from app.auth.context import use_user
    from app.integrations.tasks.routines import (
        routines_add_handler, routines_decline_handler, routines_transfer_handler,
    )

    added = _call(
        routines_add_handler, db_session, title="Bins", done_when="Done",
        schedule_kind="interval", schedule_spec="P7D", default_owner="alex",
    )
    routine_uid = added["created"]["uid"]
    _call(routines_transfer_handler, db_session, routine=routine_uid, to_user="sam")

    with use_user(2):
        out = _call(routines_decline_handler, db_session, routine=routine_uid, note="not this week")

    assert out["declined"]["pending_owner_id"] is None
    assert out["declined"]["default_owner_id"] == 1


# ── deactivation ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_deactivating_drops_the_open_round_without_a_skip_event(db_session, backlog_note):
    from app.integrations.tasks.models import Task, TaskEvent
    from app.integrations.tasks.routines import routines_add_handler, routines_update_handler

    added = _call(
        routines_add_handler, db_session, title="Bins", done_when="Done",
        schedule_kind="interval", schedule_spec="P7D",
    )
    _call(routines_update_handler, db_session, routine=added["created"]["uid"], active=False)

    round_task = db_session.query(Task).filter(Task.uid == added["first_round"]).one()
    assert round_task.status == "dropped"
    events = db_session.query(TaskEvent).filter(TaskEvent.task_id == round_task.id, TaskEvent.to_status == "dropped").all()
    assert all(e.field != "skip" for e in events)
    assert any(e.note == "routine deactivated" for e in events)


# ── tasks_query / review / dupes exclusions ─────────────────────────────────


@pytest.mark.anyio
async def test_tasks_query_routines_filter(db_session, backlog_note):
    from app.integrations.tasks.routines import routines_add_handler
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    _call(tasks_add_handler, db_session, title="An ordinary task")
    _call(
        routines_add_handler, db_session, title="A routine", done_when="Done",
        schedule_kind="interval", schedule_spec="P7D",
    )

    assert _call(tasks_query_handler, db_session, routines="all")["count"] == 2
    assert _call(tasks_query_handler, db_session, routines="only")["count"] == 1
    assert _call(tasks_query_handler, db_session, routines="exclude")["count"] == 1


@pytest.mark.anyio
async def test_tasks_review_excludes_rounds(db_session, backlog_note):
    from app.integrations.tasks.routines import routines_add_handler
    from app.integrations.tasks.tools import tasks_review_handler

    # A bare noun phrase (no verb, short) — would trip `no_action_named` if it
    # were reviewed as an ordinary task.
    _call(
        routines_add_handler, db_session, title="Kitchen bins", done_when="Bins are out",
        schedule_kind="interval", schedule_spec="P7D",
    )
    out = _call(tasks_review_handler, db_session)
    assert out["total"] == 0
    assert out["findings"] == []


@pytest.mark.anyio
async def test_duplicate_detection_excludes_rounds(db_session, backlog_note):
    """Two rounds with an IDENTICAL title (as every round of the same
    routine has) would score a perfect title-similarity match if they were
    in scope — proving the exclusion actually does something, not just that
    an empty ledger has no duplicates."""
    from app.integrations.tasks import dupes
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.routines import routines_add_handler

    added = _call(
        routines_add_handler, db_session, title="Water plants", done_when="Watered",
        schedule_kind="interval", schedule_spec="P3D",
    )
    first_round = db_session.query(Task).filter(Task.uid == added["first_round"]).one()
    # Simulates a second open round existing (the just-in-time invariant
    # normally prevents this) purely to prove the query-level exclusion, not
    # the invariant.
    db_session.add(Task(
        uid="TASK-9001", title=first_round.title, status="next",
        routine_id=first_round.routine_id, sort_order=99999,
    ))
    db_session.commit()

    pairs = dupes.duplicate_pairs(db_session)
    assert pairs == []


# ── renderer ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_renderer_has_a_routines_section_and_excludes_rounds_from_categories(db_session, backlog_note):
    from app.integrations.tasks.routines import routines_add_handler

    _call(
        routines_add_handler, db_session, title="Water plants", done_when="Watered",
        schedule_kind="interval", schedule_spec="P3D",
    )
    written = backlog_note.read_text()
    assert "# Routines" in written
    assert "Water plants" in written
    # Only one occurrence of the title: the Routines section, not also a
    # category section carrying the round as an ordinary task line.
    assert written.count("Water plants") == 1


# ── steps become a checklist ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_steps_are_copied_into_the_round_description(db_session, backlog_note):
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.routines import routines_add_handler

    added = _call(
        routines_add_handler, db_session, title="Morning routine", done_when="Ready for school",
        schedule_kind="fixed", schedule_spec="FREQ=DAILY;BYHOUR=7",
        steps=["Get dressed", "Eat breakfast", "Pack bag"],
    )
    round_task = db_session.query(Task).filter(Task.uid == added["first_round"]).one()
    assert "- [ ] Get dressed" in round_task.description
    assert "- [ ] Eat breakfast" in round_task.description
    assert "- [ ] Pack bag" in round_task.description


@pytest.mark.anyio
async def test_routine_row_returns_steps_in_order(db_session, backlog_note):
    """`routines_list`'s row didn't return the routine's own steps at all —
    only the checklist baked onto each round's description at mint time — so
    the frontend's edit sheet started every existing routine's step list
    empty. `_routine_row`'s `steps` field is the fix; this pins the order
    (`RoutineStep.ord`) and that it's plain text, not the round's markdown
    checklist."""
    from app.integrations.tasks.models import Routine
    from app.integrations.tasks.routines import _routine_row, routines_add_handler

    added = _call(
        routines_add_handler, db_session, title="Morning routine", done_when="Ready for school",
        schedule_kind="fixed", schedule_spec="FREQ=DAILY;BYHOUR=7",
        steps=["Get dressed", "Eat breakfast", "Pack bag"],
    )
    routine = db_session.query(Routine).filter(Routine.uid == added["created"]["uid"]).one()
    row = _routine_row(db_session, routine)
    assert row["steps"] == ["Get dressed", "Eat breakfast", "Pack bag"]


# ── mutation check: reinstate the SQL NULL-IN bug and confirm it fails ──────


@pytest.mark.anyio
async def test_last_closed_at_reinstated_bug_would_miss_ordinary_completions(db_session, backlog_note):
    """`field IN (NULL, 'skip')` is not the same test as `field IS NULL OR
    field = 'skip'` — SQL's IN never matches a NULL row against a NULL in the
    list. This test pins the correct behaviour (a plain completion, field
    NULL, counts as a close) so that regressing to `.in_((None, "skip"))`
    would fail it.
    """
    from app.integrations.tasks.routines import _routine_row, routines_add_handler
    from app.integrations.tasks.tools import tasks_complete_handler

    added = _call(
        routines_add_handler, db_session, title="Water plants", done_when="Watered",
        schedule_kind="interval", schedule_spec="P3D",
    )
    _call(tasks_complete_handler, db_session, uid=added["first_round"])

    from app.integrations.tasks.models import Routine
    routine = db_session.query(Routine).filter(Routine.uid == added["created"]["uid"]).one()
    row = _routine_row(db_session, routine)
    assert row["last_closed_at"] is not None
