"""The task ledger's write path.

Every write re-renders `Task Backlog.md`. That is not decoration: from the
moment the file became a view, a change that does not re-render is a change
nobody can see.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db

SAMPLE = """---
title: Task Backlog
---

# Backlog — by category

# Home

## [[Malahide House]]

- [ ] **Wall mount the TV** 🔺 #home #quick
- [ ] **Collect the seed** ⏫ #home #errand 📅 2026-09-02
"""


@pytest.fixture
def ledger(db_session, tmp_path, monkeypatch):
    """An imported ledger whose rendered view lands in a temp file."""
    from tests.legacy_backlog_importer import import_backlog

    from app.auth.context import current_user_id

    # One file PER USER, as production has them. A single shared path used to
    # do, but each user's file now has its own content (2026-09-06), so
    # user 2 writing to a shared path would silently overwrite user 1's.
    def resolve(path, user_id_override=None):
        uid = user_id_override if user_id_override is not None else current_user_id()
        d = tmp_path / f"user{uid}"
        d.mkdir(exist_ok=True)
        return d / "Task Backlog.md"

    note = resolve("Task Backlog.md", 1)  # the default caller's
    note.write_text("placeholder\n")
    monkeypatch.setattr("app.services.vault_paths.resolve", resolve)
    import_backlog(db_session, SAMPLE)
    return note


def _call(handler, session, **args):
    return json.loads(handler(session, args))


@pytest.mark.anyio
async def test_query_returns_open_tasks_by_default(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler

    out = _call(tasks_query_handler, db_session, owner="household")

    assert out["count"] == 2
    assert {t["uid"] for t in out["tasks"]} == {"TASK-0001", "TASK-0002"}
    assert out["tasks"][0]["project"] == "Malahide House"


@pytest.mark.anyio
async def test_query_filters_the_way_the_file_s_lenses_do(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler

    assert _call(tasks_query_handler, db_session, owner="household", priority="highest")["count"] == 1
    assert _call(tasks_query_handler, db_session, owner="household", context="errand")["count"] == 1
    assert _call(tasks_query_handler, db_session, owner="household", energy="quick")["count"] == 1
    assert _call(tasks_query_handler, db_session, owner="household", tag="#home")["count"] == 2
    assert _call(tasks_query_handler, db_session, owner="household", text="seed")["count"] == 1


# ── 2026-09-04: someday/dropped must never leak into "open" ────────────────
#
# Root cause of the reported bug: `include_done` used to REMOVE the status
# filter entirely rather than adding "done" to it, so a someday/dropped row
# rendered indistinguishable from next/waiting/inbox/scheduled. The frontend
# only ever filtered out "done", so a row moved to Someday would optimistically
# disappear and then reappear the moment the list reconciled against the
# server, because the server had handed it back as if it were open.


@pytest.mark.anyio
async def test_include_done_returns_open_and_done_never_someday_or_dropped(db_session, ledger):
    """Mutation-check: reinstating the old `elif not include_done` behaviour
    (which drops the status filter entirely) makes this fail, because
    someday/dropped would then be included alongside next/done."""
    from app.integrations.tasks.tools import (
        tasks_add_handler, tasks_query_handler, tasks_update_handler,
    )

    someday = _call(tasks_add_handler, db_session, title="Learn the bouzouki",
                     status="someday")["created"]["uid"]
    dropped = _call(tasks_add_handler, db_session, title="Buy a second shed",
                     status="next")["created"]["uid"]
    _call(tasks_update_handler, db_session, uid=dropped, status="dropped")
    done = _call(tasks_add_handler, db_session, title="Book the boiler service",
                  status="next")["created"]["uid"]
    _call(tasks_update_handler, db_session, uid=done, status="done")

    out = _call(tasks_query_handler, db_session, owner="household", include_done=True)
    uids = {t["uid"] for t in out["tasks"]}

    assert someday not in uids
    assert dropped not in uids
    assert done in uids
    # The two originally-imported open rows plus the newly-done one.
    assert uids == {"TASK-0001", "TASK-0002", done}


@pytest.mark.anyio
async def test_statuses_filter_selects_an_explicit_subset(db_session, ledger):
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    someday = _call(tasks_add_handler, db_session, title="Learn the bouzouki",
                     status="someday")["created"]["uid"]
    _call(tasks_add_handler, db_session, title="Buy a second shed", status="waiting")

    out = _call(tasks_query_handler, db_session, owner="household", statuses=["someday"])
    assert {t["uid"] for t in out["tasks"]} == {someday}

    out_both = _call(tasks_query_handler, db_session, owner="household", statuses=["someday", "waiting"])
    assert out_both["count"] == 2


@pytest.mark.anyio
async def test_statuses_filter_rejects_unknown_status(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler

    with pytest.raises(ValueError):
        _call(tasks_query_handler, db_session, owner="household", statuses=["not-a-real-status"])


@pytest.mark.anyio
async def test_completed_since_scopes_done_rows_without_competing_with_open(db_session, ledger):
    """The loops BFF's fix for the 200-row cap: ask for open rows and
    today's done rows as two separate queries rather than one `include_done`
    call whose limit the done rows could crowd out. completed_since is the
    filter that makes the second query possible."""
    from app.integrations.tasks.tools import (
        tasks_add_handler, tasks_query_handler, tasks_update_handler,
    )

    old_done = _call(tasks_add_handler, db_session, title="Renew passport", status="next")["created"]["uid"]
    _call(tasks_update_handler, db_session, uid=old_done, status="done")
    from app.integrations.tasks.models import Task
    task = db_session.query(Task).filter(Task.uid == old_done).one()
    task.completed_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    db_session.commit()

    today_done = _call(tasks_add_handler, db_session, title="Book the boiler service", status="next")["created"]["uid"]
    _call(tasks_update_handler, db_session, uid=today_done, status="done")

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    out = _call(tasks_query_handler, db_session, owner="household", statuses=["done"], completed_since=cutoff)

    uids = {t["uid"] for t in out["tasks"]}
    assert today_done in uids
    assert old_done not in uids


@pytest.mark.anyio
async def test_add_records_a_real_created_at(db_session, ledger):
    """Imported tasks have created_at NULL because it is unknowable. A task
    created here genuinely has one — this is where the aging clock starts."""
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_add_handler

    out = _call(tasks_add_handler, db_session, title="Book the boiler service",
                priority="high", context="errand", tags=["#home"])

    assert out["created"]["uid"] == "TASK-0003"
    assert out["created"]["created_at"] is not None
    imported = db_session.query(Task).filter(Task.uid == "TASK-0001").one()
    assert imported.created_at is None


# ── 2026-09-04: tasks_add defaults owner_id to the caller ──────────────────
#
# Backlog item: three tasks landed via tasks_add with owner_id null and had
# to be claimed by SQL — the same gap a 135-row backfill closed on 2026-09-03.
# tasks_add has no `owner` argument (a hand-over is tasks_transfer, request/
# accept — see the tasks_transfer tests below); the only fix available here
# is a sane default.


@pytest.mark.anyio
async def test_add_defaults_owner_to_the_caller(db_session, ledger):
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_add_handler

    out = _call(tasks_add_handler, db_session, title="Book the boiler service")

    assert out["created"]["owner_id"] == 1  # the autouse fixture binds user 1 (alex)
    stored = db_session.query(Task).filter(Task.uid == out["created"]["uid"]).one()
    assert stored.owner_id == 1


@pytest.mark.anyio
async def test_add_has_no_owner_argument_a_transfer_would_bypass(db_session, ledger):
    """tasks_add must not accept an arbitrary owner — that would set someone
    else's task without their accept, the exact thing tasks_transfer/
    tasks_accept exist to gate. Passing one is simply ignored, not honoured."""
    from app.integrations.tasks.tools import tasks_add_handler

    out = _call(tasks_add_handler, db_session, title="Do the thing", owner="sam")

    assert out["created"]["owner_id"] == 1
    assert out["created"]["pending_owner_id"] is None


@pytest.mark.anyio
async def test_add_records_owner_id_as_a_field_event(db_session, ledger):
    """The default is recorded the same way any other owner_id set is — via
    task_events — not as a silent side effect invisible to tasks_history."""
    from app.integrations.tasks.models import Task, TaskEvent
    from app.integrations.tasks.tools import tasks_add_handler

    out = _call(tasks_add_handler, db_session, title="Book the boiler service")

    event = (
        db_session.query(TaskEvent)
        .filter(TaskEvent.field == "owner_id")
        .filter(TaskEvent.new_value == "1")
        .one()
    )
    task_id = db_session.query(Task).filter(Task.uid == out["created"]["uid"]).one().id
    assert event.task_id == task_id
    assert event.old_value is None


@pytest.mark.anyio
async def test_split_inherits_the_originals_owner(db_session, ledger):
    """A split half is a continuation of the same work, so it inherits the
    original's owner like project/priority/context/tags — not the unowned
    gap tasks_add used to leave."""
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_split_handler, tasks_update_handler

    uid = next(t.uid for t in db_session.query(Task).all() if "Wall mount" in t.title)
    _call(tasks_update_handler, db_session, uid=uid, status="next")

    from app.auth.context import use_user
    with use_user(2):
        out = _call(tasks_split_handler, db_session, uid=uid,
                    parts=["Buy the bracket", "Mount the TV"])

    assert out["created"][0]["owner_id"] == 2


@pytest.mark.anyio
async def test_split_defaults_to_the_caller_when_the_original_has_no_owner(db_session, ledger):
    """An imported task predating the default (owner_id null) should not
    mint another unowned open loop when split — fall back to the caller,
    same rule as tasks_add."""
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_split_handler

    uid = next(t.uid for t in db_session.query(Task).all() if "Wall mount" in t.title)
    assert db_session.query(Task).filter(Task.uid == uid).one().owner_id is None

    out = _call(tasks_split_handler, db_session, uid=uid,
                parts=["Buy the bracket", "Mount the TV"])

    assert out["created"][0]["owner_id"] == 1


@pytest.mark.anyio
async def test_review_reports_unowned_open_loops(db_session, ledger):
    """The hygiene report a client renders owner chips against a `Problems`
    section for: a count + list of open tasks with no owner. Imported tasks
    have owner_id null by construction (import doesn't know who owns them),
    so both ledger tasks should show up here until claimed or handed over."""
    from app.integrations.tasks.tools import tasks_add_handler, tasks_review_handler

    out = _call(tasks_review_handler, db_session, owner="household")

    assert out["unowned_open"]["count"] == 2
    assert {t["uid"] for t in out["unowned_open"]["tasks"]} == {"TASK-0001", "TASK-0002"}

    # A task added through tasks_add (owner defaulted to the caller) must
    # NOT appear — the whole point of the default is that it doesn't.
    _call(tasks_add_handler, db_session, title="Book the boiler service")
    out = _call(tasks_review_handler, db_session, owner="household")
    assert out["unowned_open"]["count"] == 2
    assert "TASK-0003" not in {t["uid"] for t in out["unowned_open"]["tasks"]}


@pytest.mark.anyio
async def test_every_write_rerenders_the_file(db_session, ledger):
    from app.integrations.tasks.tools import tasks_add_handler

    assert "placeholder" in ledger.read_text()

    _call(tasks_add_handler, db_session, title="Book the boiler service")

    written = ledger.read_text()
    assert "placeholder" not in written
    assert "Book the boiler service" in written


@pytest.mark.anyio
async def test_status_changes_record_an_event(db_session, ledger):
    from app.integrations.tasks.models import TaskEvent
    from app.integrations.tasks.tools import tasks_complete_handler

    out = _call(tasks_complete_handler, db_session, uid="TASK-0001", note="done Sunday")

    assert out["completed"]["status"] == "done"
    assert out["completed"]["completed_at"] is not None
    event = (
        db_session.query(TaskEvent)
        .filter(TaskEvent.to_status == "done", TaskEvent.note == "done Sunday")
        .one()
    )
    assert event.from_status == "next"


@pytest.mark.anyio
async def test_reopening_clears_the_completion(db_session, ledger):
    from app.integrations.tasks.tools import tasks_complete_handler, tasks_update_handler

    _call(tasks_complete_handler, db_session, uid="TASK-0001")
    out = _call(tasks_update_handler, db_session, uid="TASK-0001", status="next")

    assert out["updated"]["status"] == "next"
    assert out["updated"]["completed_at"] is None


@pytest.mark.anyio
async def test_bulk_update_reports_failures_instead_of_losing_good_changes(db_session, ledger):
    """In a sweep, 39 good changes must not be lost because one uid was
    mistyped."""
    from app.integrations.tasks.tools import tasks_bulk_update_handler

    out = _call(tasks_bulk_update_handler, db_session, updates=[
        {"uid": "TASK-0001", "priority": "low"},
        {"uid": "TASK-9999", "priority": "low"},
        {"uid": "TASK-0002", "status": "someday"},
    ])

    assert out["updated"] == ["TASK-0001", "TASK-0002"]
    assert len(out["failed"]) == 1
    assert out["failed"][0]["uid"] == "TASK-9999"

    from app.integrations.tasks.models import Task
    assert db_session.query(Task).filter(Task.uid == "TASK-0001").one().priority == "low"


@pytest.mark.anyio
async def test_bulk_update_renders_once_not_per_task(db_session, ledger, monkeypatch):
    """A sweep touching 40 tasks would otherwise rewrite the file 40 times, and
    every intermediate version is a state the backlog was never in."""
    from app.integrations.tasks import tools

    calls = []
    monkeypatch.setattr(tools, "_render", lambda session: calls.append(1))

    _call(tools.tasks_bulk_update_handler, db_session, updates=[
        {"uid": "TASK-0001", "priority": "low"},
        {"uid": "TASK-0002", "priority": "low"},
    ])

    assert len(calls) == 1


@pytest.mark.anyio
async def test_an_unknown_project_is_refused_with_the_valid_ones(db_session, ledger):
    from app.integrations.tasks.tools import tasks_add_handler

    with pytest.raises(ValueError, match="Malahide House"):
        _call(tasks_add_handler, db_session, title="x", project="No Such Project")


@pytest.mark.anyio
async def test_unknown_uid_is_refused(db_session, ledger):
    from app.integrations.tasks.tools import tasks_update_handler

    with pytest.raises(ValueError, match="TASK-9999"):
        _call(tasks_update_handler, db_session, uid="TASK-9999", priority="low")


# ── 2026-09-03: dated notes, updated_at, and tag namespaces ────────────────


@pytest.mark.anyio
async def test_rows_carry_updated_at(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler

    row = _call(tasks_query_handler, db_session, owner="household")["tasks"][0]
    assert "updated_at" in row


@pytest.mark.anyio
async def test_notes_are_dated_append_only_entries(db_session, ledger):
    from app.integrations.tasks.tools import (
        tasks_note_add_handler, tasks_notes_handler, tasks_query_handler,
    )

    assert _call(tasks_notes_handler, db_session, uid="TASK-0001")["count"] == 0

    first = _call(tasks_note_add_handler, db_session, uid="TASK-0001", body="Rang the fitter; Tuesday.")
    second = _call(tasks_note_add_handler, db_session, uid="TASK-0001", body="Tuesday slipped to Thursday.")
    assert first["count"] == 1 and second["count"] == 2
    assert first["added"]["at"] is not None

    notes = _call(tasks_notes_handler, db_session, uid="TASK-0001")["notes"]
    assert [n["body"] for n in notes] == ["Rang the fitter; Tuesday.", "Tuesday slipped to Thursday."]

    # The standing summary is untouched — notes are a separate record.
    row = next(t for t in _call(tasks_query_handler, db_session, owner="household")["tasks"] if t["uid"] == "TASK-0001")
    assert row["description"] is None

    with pytest.raises(ValueError):
        _call(tasks_note_add_handler, db_session, uid="TASK-0001", body="   ")


@pytest.mark.anyio
async def test_a_note_shows_in_history_without_changing_status(db_session, ledger):
    from app.integrations.tasks.loops import tasks_history_handler
    from app.integrations.tasks.tools import tasks_note_add_handler

    _call(tasks_note_add_handler, db_session, uid="TASK-0001", body="Decided: keep the original pattern.")
    events = _call(tasks_history_handler, db_session, uid="TASK-0001")["events"]
    note_events = [e for e in events if e["field"] == "note"]
    assert len(note_events) == 1
    assert note_events[0]["to"].startswith("Decided:")
    assert not any(e["field"] == "status" and e["to"] == "done" for e in events)


@pytest.mark.anyio
async def test_tag_prefix_matches_every_claude_role(db_session, ledger):
    from app.integrations.tasks.models import CLAUDE_TAG_PREFIX, CLAUDE_TAGS
    from app.integrations.tasks.tools import tasks_query_handler, tasks_update_handler

    _call(tasks_update_handler, db_session, uid="TASK-0001", tags=["#home", CLAUDE_TAGS["do"]])
    _call(tasks_update_handler, db_session, uid="TASK-0002", tags=["#home", CLAUDE_TAGS["investigate"]])

    assert _call(tasks_query_handler, db_session, owner="household", tag_prefix=CLAUDE_TAG_PREFIX)["count"] == 2
    assert _call(tasks_query_handler, db_session, owner="household", tag=CLAUDE_TAGS["fix"])["count"] == 0
    # A prefix is a prefix: '#ho' is not a tag anyone typed, but it must not match '#home' by accident either? It does, and that is fine — the caller asked for a namespace.
    assert _call(tasks_query_handler, db_session, owner="household", tag_prefix="#claude/inv")["count"] == 1


# ── 2026-09-03: multi-user loops — transfer/accept/decline, nudge ──────────
#
# Transfer is a REQUEST, not a write ("TCP not UDP"). The autouse fixture
# binds current_user_id() to user 1 (alex); these tests switch to user 2
# (sam) with `use_user` to exercise the pending-owner-only guards.


@pytest.mark.anyio
async def test_transfer_sets_pending_and_does_not_move_owner(db_session, ledger):
    from app.integrations.tasks.tools import tasks_transfer_handler

    out = _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")

    assert out["transferred"]["pending_owner_id"] == 2
    assert out["transferred"]["owner_id"] is None
    assert out["transferred"]["pending_since"] is not None

    from app.integrations.tasks.models import Task
    assert db_session.query(Task).filter(Task.uid == "TASK-0001").one().owner_id is None


@pytest.mark.anyio
async def test_accept_moves_pending_owner_into_owner(db_session, ledger):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_accept_handler, tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")

    with use_user(2):
        out = _call(tasks_accept_handler, db_session, uid="TASK-0001")

    assert out["accepted"]["owner_id"] == 2
    assert out["accepted"]["pending_owner_id"] is None
    assert out["accepted"]["pending_since"] is None


@pytest.mark.anyio
async def test_only_the_pending_owner_can_accept(db_session, ledger):
    """The negative case: alex (user 1) requested sam (user 2) take it, so
    alex accepting his own request must be refused — reinstating the missing
    guard (dropping the `pending_owner_id != me` check) makes this pass
    wrongly, which is what would have shipped a hijack-by-anyone-logged-in
    bug."""
    from app.integrations.tasks.tools import tasks_accept_handler, tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")

    with pytest.raises(ValueError, match="no transfer pending"):
        _call(tasks_accept_handler, db_session, uid="TASK-0001")

    from app.integrations.tasks.models import Task
    task = db_session.query(Task).filter(Task.uid == "TASK-0001").one()
    assert task.owner_id is None
    assert task.pending_owner_id == 2


@pytest.mark.anyio
async def test_only_the_pending_owner_can_decline(db_session, ledger):
    from app.integrations.tasks.tools import tasks_decline_handler, tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")

    with pytest.raises(ValueError, match="no transfer pending"):
        _call(tasks_decline_handler, db_session, uid="TASK-0001")


@pytest.mark.anyio
async def test_decline_clears_pending_without_moving_owner_and_records_note(db_session, ledger):
    from app.auth.context import use_user
    from app.integrations.tasks.models import TaskComment, Task
    from app.integrations.tasks.tools import tasks_decline_handler, tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    with use_user(2):
        out = _call(tasks_decline_handler, db_session, uid="TASK-0001", note="not this week")

    assert out["declined"]["pending_owner_id"] is None
    assert out["declined"]["owner_id"] is None

    task = db_session.query(Task).filter(Task.uid == "TASK-0001").one()
    assert db_session.query(TaskComment).filter(TaskComment.task_id == task.id, TaskComment.body == "not this week").count() == 1


@pytest.mark.anyio
async def test_transferring_again_redirects_the_pending_request(db_session, ledger):
    from app.integrations.tasks.tools import tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    out = _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="alex")

    assert out["transferred"]["pending_owner_id"] == 1


@pytest.mark.anyio
async def test_nudge_records_event_and_comment_and_row_count(db_session, ledger):
    from app.integrations.tasks.tools import tasks_nudge_handler, tasks_query_handler

    out = _call(tasks_nudge_handler, db_session, uid="TASK-0001", note="still ok for Friday?")

    assert out["nudged"]["nudges"] == 1
    assert out["nudged"]["last_nudged_at"] is not None

    from app.integrations.tasks.models import TaskComment
    assert (
        db_session.query(TaskComment)
        .filter(TaskComment.body.like("status update?%"))
        .count() == 1
    )

    row = next(t for t in _call(tasks_query_handler, db_session, owner="household")["tasks"] if t["uid"] == "TASK-0001")
    assert row["nudges"] == 1


@pytest.mark.anyio
async def test_query_owner_pending_for_me_and_blocked_on_me(db_session, ledger):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import (
        tasks_accept_handler, tasks_query_handler, tasks_transfer_handler,
    )

    # TASK-0001 owned by alex (user 1); TASK-0002 owned by sam (user 2) and
    # blocked by TASK-0001 — so TASK-0001 is "blocked_on_me" for alex.
    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="alex")
    with use_user(1):
        _call(tasks_accept_handler, db_session, uid="TASK-0001")
    _call(tasks_transfer_handler, db_session, uid="TASK-0002", to_user="sam")
    with use_user(2):
        _call(tasks_accept_handler, db_session, uid="TASK-0002")

    from app.integrations.tasks.tools import tasks_block_handler
    _call(tasks_block_handler, db_session, owner="household", action="add", uid="TASK-0002", blocked_by="TASK-0001")

    assert _call(tasks_query_handler, db_session, owner="alex")["count"] == 1
    assert _call(tasks_query_handler, db_session, owner="me")["count"] == 1
    assert _call(tasks_query_handler, db_session, owner="sam")["count"] == 1

    blocked_on_me = _call(tasks_query_handler, db_session, owner="household", blocked_on_me=True)
    assert {t["uid"] for t in blocked_on_me["tasks"]} == {"TASK-0001"}

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    pending = _call(tasks_query_handler, db_session, owner="household", pending_for_me=True)
    assert {t["uid"] for t in pending["tasks"]} == set()  # caller is alex (user 1), pending is for sam

    with use_user(2):
        pending_for_sam = _call(tasks_query_handler, db_session, owner="household", pending_for_me=True)
    assert {t["uid"] for t in pending_for_sam["tasks"]} == {"TASK-0001"}


@pytest.mark.anyio
async def test_unknown_owner_is_refused_with_the_valid_ones(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler

    with pytest.raises(ValueError, match="sam"):
        _call(tasks_query_handler, db_session, owner="nobody")


# ── whoami ──────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_whoami_returns_the_caller_not_a_hardcoded_user(db_session, ledger):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_whoami_handler

    out = _call(tasks_whoami_handler, db_session)
    assert out == {"id": 1, "name": "alex", "display_name": "Alex"}

    with use_user(2):
        out = _call(tasks_whoami_handler, db_session)
    assert out["id"] == 2 and out["name"] == "sam"


# ── blocking_of: the reverse, unlimited view most_blocking caps ────────────


@pytest.mark.anyio
async def test_blocking_of_reports_every_task_a_blocker_holds_up(db_session, ledger):
    from app.integrations.tasks.tools import tasks_block_handler

    _call(tasks_block_handler, db_session, owner="household", action="add", uid="TASK-0002", blocked_by="TASK-0001")
    out = _call(tasks_block_handler, db_session, owner="household")

    assert out["blocking_of"]["TASK-0001"] == [{"uid": "TASK-0002", "title": "**Collect the seed**"}]


# ── notifications: nudge / transfer / accept / decline ─────────────────────
#
# These monkeypatch `get_capability` at its home module so `tools.py`'s local
# `from app.plugin.capabilities import get_capability` import picks up the
# stub. A `send` call is recorded as (title, body, severity, user_id).


class _FakeNotify:
    def __init__(self):
        self.sent: list[tuple[str, str, str, int | None]] = []

    def send(self, title, body, severity="warning", user_id=None, **kwargs):
        self.sent.append((title, body, severity, user_id))
        return True


@pytest.fixture
def fake_notify(monkeypatch):
    fake = _FakeNotify()
    import app.plugin.capabilities as capabilities

    monkeypatch.setattr(capabilities, "get_capability", lambda name: fake)
    return fake


@pytest.mark.anyio
async def test_transfer_notifies_the_new_pending_owner(db_session, ledger, fake_notify):
    from app.integrations.tasks.tools import tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")

    assert len(fake_notify.sent) == 1
    title, body, severity, user_id = fake_notify.sent[0]
    assert user_id == 2  # sam
    assert "Alex" in title
    assert "TASK-0001" in body


@pytest.mark.anyio
async def test_accept_notifies_the_original_requester(db_session, ledger, fake_notify):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_accept_handler, tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    fake_notify.sent.clear()

    with use_user(2):
        _call(tasks_accept_handler, db_session, uid="TASK-0001")

    assert len(fake_notify.sent) == 1
    title, body, severity, user_id = fake_notify.sent[0]
    assert user_id == 1  # alex, who requested the transfer
    assert "Sam" in title


@pytest.mark.anyio
async def test_decline_notifies_the_original_requester_with_the_note(db_session, ledger, fake_notify):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_decline_handler, tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    fake_notify.sent.clear()

    with use_user(2):
        _call(tasks_decline_handler, db_session, uid="TASK-0001", note="not this week")

    assert len(fake_notify.sent) == 1
    title, body, severity, user_id = fake_notify.sent[0]
    assert user_id == 1
    assert "not this week" in body


@pytest.mark.anyio
async def test_nudge_notifies_the_owner(db_session, ledger, fake_notify):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import (
        tasks_accept_handler, tasks_nudge_handler, tasks_transfer_handler,
    )

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    with use_user(2):
        _call(tasks_accept_handler, db_session, uid="TASK-0001")
    fake_notify.sent.clear()

    out = _call(tasks_nudge_handler, db_session, uid="TASK-0001")

    assert out["notified"] is True
    assert len(fake_notify.sent) == 1
    assert fake_notify.sent[0][3] == 2  # sam owns it


@pytest.mark.anyio
async def test_a_repeat_nudge_within_the_cooldown_does_not_notify_again(
    db_session, ledger, fake_notify, monkeypatch,
):
    """The ledger records every nudge; the push is what's rate-limited."""
    from app.auth.context import use_user
    from app.integrations.tasks import tools
    from app.integrations.tasks.tools import (
        tasks_accept_handler, tasks_nudge_handler, tasks_transfer_handler,
    )

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    with use_user(2):
        _call(tasks_accept_handler, db_session, uid="TASK-0001")
    fake_notify.sent.clear()

    first = _call(tasks_nudge_handler, db_session, uid="TASK-0001")
    second = _call(tasks_nudge_handler, db_session, uid="TASK-0001")

    assert first["notified"] is True
    assert second["notified"] is False
    assert len(fake_notify.sent) == 1
    # The ledger still recorded both nudges — only the push was throttled.
    assert second["nudged"]["nudges"] == 2


@pytest.mark.anyio
async def test_nudging_your_own_task_does_not_notify_yourself(db_session, ledger, fake_notify):
    from app.integrations.tasks.tools import tasks_accept_handler, tasks_nudge_handler, tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="alex")
    from app.auth.context import use_user
    with use_user(1):
        _call(tasks_accept_handler, db_session, uid="TASK-0001")

    out = _call(tasks_nudge_handler, db_session, uid="TASK-0001")
    assert out["notified"] is False
    assert fake_notify.sent == []



@pytest.mark.anyio
async def test_handed_by_me_shows_loops_i_gave_away_until_they_come_back(db_session, ledger):
    """Sam's first feedback (2026-09-06): handing a loop over must not make
    it vanish. `handed_by_me` is derived from the transfer events, so it needs
    no new column and follows the loop through accept and completion."""
    from app.auth.context import use_user
    from app.integrations.tasks.tools import (
        tasks_accept_handler, tasks_query_handler, tasks_transfer_handler,
    )

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    # Pending: still mine, so not yet "handed"; the inbox lens on her side has it.
    assert _call(tasks_query_handler, db_session, owner="household", handed_by_me=True)["count"] == 0
    with use_user(2):
        _call(tasks_accept_handler, db_session, uid="TASK-0001")
    mine = _call(tasks_query_handler, db_session, owner="household", handed_by_me=True)
    assert [t["uid"] for t in mine["tasks"]] == ["TASK-0001"]
    assert mine["tasks"][0]["owner_id"] == 2
    # From her side it is simply hers; she handed nothing.
    with use_user(2):
        assert _call(tasks_query_handler, db_session, owner="household", handed_by_me=True)["count"] == 0
        # Handed back: it leaves my "handed" view once I own it again.
        _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="alex")
    _call(tasks_accept_handler, db_session, uid="TASK-0001")
    assert _call(tasks_query_handler, db_session, owner="household", handed_by_me=True)["count"] == 0


@pytest.mark.anyio
async def test_pending_row_says_who_handed_it_over_even_when_nobody_owns_it(db_session, ledger):
    """Sam's first feedback (2026-09-06), receiving side: a hand-over must
    not be blind. The row used to give the receiver only `owner_id`, which is
    NULL for an unowned loop and is not the requester anyway — the sender is
    the actor of the transfer-request event, surfaced as `pending_from_id`."""
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_query_handler, tasks_transfer_handler

    out = _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    assert out["transferred"]["owner_id"] is None  # the owner is no help here
    assert out["transferred"]["pending_from_id"] == 1

    with use_user(2):
        inbox = _call(tasks_query_handler, db_session, owner="household", pending_for_me=True)
    assert [(t["uid"], t["pending_from_id"]) for t in inbox["tasks"]] == [("TASK-0001", 1)]
    # A loop with nothing pending carries no sender at all.
    assert all(
        t["pending_from_id"] is None
        for t in _call(tasks_query_handler, db_session, owner="household")["tasks"] if t["uid"] != "TASK-0001"
    )


@pytest.mark.anyio
async def test_redirecting_a_transfer_repoints_who_handed_it_over(db_session, ledger):
    """The person now asking is whoever made the LATEST request: alex hands
    it to sam, sam redirects it back to alex — alex's inbox must say it
    came from Sam, not from himself."""
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_query_handler, tasks_transfer_handler

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    with use_user(2):
        out = _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="alex")
    assert out["transferred"]["pending_owner_id"] == 1
    assert out["transferred"]["pending_from_id"] == 2
    inbox = _call(tasks_query_handler, db_session, owner="household", pending_for_me=True)
    assert [t["pending_from_id"] for t in inbox["tasks"]] == [2]


@pytest.mark.anyio
async def test_who_handed_it_over_clears_once_the_transfer_is_answered(db_session, ledger):
    """The request event stays in the ledger forever (that is what
    `handed_by_me` and `tasks_history` read), but `pending_from_id` describes
    the OUTSTANDING request only — accepting or declining must blank it."""
    from app.auth.context import use_user
    from app.integrations.tasks.tools import (
        tasks_accept_handler, tasks_decline_handler, tasks_transfer_handler,
    )

    _call(tasks_transfer_handler, db_session, uid="TASK-0001", to_user="sam")
    with use_user(2):
        accepted = _call(tasks_accept_handler, db_session, uid="TASK-0001")
    assert accepted["accepted"]["pending_from_id"] is None

    _call(tasks_transfer_handler, db_session, uid="TASK-0002", to_user="sam")
    with use_user(2):
        declined = _call(tasks_decline_handler, db_session, uid="TASK-0002")
    assert declined["declined"]["pending_from_id"] is None


@pytest.mark.anyio
async def test_unfiled_returns_only_tasks_with_no_project(db_session, ledger):
    from app.integrations.tasks.loops import tasks_project_handler
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    _call(tasks_project_handler, db_session, title="Shed", done_when="x")
    _call(tasks_add_handler, db_session, title="Paint shed door", project="Shed")
    _call(tasks_add_handler, db_session, title="Order bobbins")
    out = _call(tasks_query_handler, db_session, unfiled=True, owner="me")
    titles = {t["title"] for t in out["tasks"]}
    assert "Order bobbins" in titles and "Paint shed door" not in titles
    assert all(t["project"] is None for t in out["tasks"])


# ── 2026-09-06: the summary strip's counts are the caller's, like the lenses ─
#
# PR #104 scoped every Loops lens to the caller's own loops, but the strip's
# `needs_rewriting` and `blocked` counts came from tasks_review / tasks_block
# with no owner — so Sam's "needs rewriting" pill counted Alex's lines over
# a lens listing only hers. Both tools now take the same `owner` argument
# tasks_query does.


@pytest.mark.anyio
async def test_review_scopes_findings_to_an_owner_but_never_unowned_open(db_session, ledger):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_add_handler, tasks_review_handler

    # Two container lines, one per person; the imported sample tasks are unowned.
    _call(tasks_add_handler, db_session, title="Sort out the garage")
    with use_user(2):
        _call(tasks_add_handler, db_session, title="Look into the boiler warranty")

    everyone = _call(tasks_review_handler, db_session, owner="household")
    assert {f["title"] for f in everyone["findings"]} == {"Sort out the garage", "Look into the boiler warranty"}

    hers = _call(tasks_review_handler, db_session, owner="sam")
    assert [f["title"] for f in hers["findings"]] == ["Look into the boiler warranty"]
    # `flagged` is the count the caveat ratio is built on — it must follow the scope too.
    assert hers["flagged"] == 1 and hers["total"] == 1
    with use_user(2):
        assert [f["title"] for f in _call(tasks_review_handler, db_session, owner="me")["findings"]] == ["Look into the boiler warranty"]
    # An unowned loop is nobody's, so that hygiene count is the household's whatever the scope.
    assert hers["unowned_open"]["count"] == everyone["unowned_open"]["count"] == 2
    with pytest.raises(ValueError, match="Unknown user"):
        _call(tasks_review_handler, db_session, owner="nobody")


@pytest.mark.anyio
async def test_block_list_scopes_the_blocked_side_to_an_owner(db_session, ledger):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_add_handler, tasks_block_handler

    mine = _call(tasks_add_handler, db_session, title="Paint the shed door")["created"]["uid"]
    with use_user(2):
        hers = _call(tasks_add_handler, db_session, title="Order the shed paint")["created"]["uid"]
    # Both wait on the unowned TASK-0001; hers also waits on mine.
    _call(tasks_block_handler, db_session, owner="household", action="add", uid=mine, blocked_by="TASK-0001")
    _call(tasks_block_handler, db_session, owner="household", action="add", uid=hers, blocked_by="TASK-0001")
    _call(tasks_block_handler, db_session, owner="household", action="add", uid=hers, blocked_by=mine)

    household = _call(tasks_block_handler, db_session, owner="household")
    assert set(household["blocked"]) == {mine, hers}

    scoped = _call(tasks_block_handler, db_session, owner="sam")
    # Her blocked loops only — whoever owns the blocker.
    assert scoped["blocked"] == {hers: sorted([mine, "TASK-0001"])}
    # And the other two views are derived from that same scoped set.
    assert {b["uid"] for b in scoped["most_blocking"]} == {mine, "TASK-0001"}
    assert all(b["blocking"] == [hers] for b in scoped["most_blocking"])
    assert {k: [t["uid"] for t in v] for k, v in scoped["blocking_of"].items()} == {mine: [hers], "TASK-0001": [hers]}

    alex = _call(tasks_block_handler, db_session, owner="me")
    assert alex["blocked"] == {mine: ["TASK-0001"]}
    assert alex["blocking_of"] == {"TASK-0001": [{"uid": mine, "title": "Paint the shed door"}]}


# ─── kind + severity: the ledger IS the register of bugs and features ──────


@pytest.mark.anyio
async def test_kind_defaults_to_task_and_severity_to_null(db_session, ledger):
    """Every pre-existing row and every plain add reads as an ordinary task —
    the register (S5.3) rides on the ledger without changing what was there."""
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    out = _call(tasks_add_handler, db_session, title="Plain task")
    assert out["created"]["kind"] == "task"
    assert out["created"]["severity"] is None
    imported = _call(tasks_query_handler, db_session, owner="household")["tasks"]
    assert {t["kind"] for t in imported} == {"task"}
    assert all("severity" in t for t in imported)


@pytest.mark.anyio
async def test_add_a_bug_with_a_severity_and_query_by_either(db_session, ledger):
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    bug = _call(tasks_add_handler, db_session, title="Render drops the marker", kind="bug", severity="high")
    _call(tasks_add_handler, db_session, title="Dark mode for the panel", kind="feature", severity="low")
    _call(tasks_add_handler, db_session, title="Rotate the logs", kind="chore")
    assert bug["created"]["kind"] == "bug"
    assert bug["created"]["severity"] == "high"

    bugs = _call(tasks_query_handler, db_session, kind="bug")["tasks"]
    assert [t["title"] for t in bugs] == ["Render drops the marker"]
    high = _call(tasks_query_handler, db_session, severity="high")["tasks"]
    assert [t["uid"] for t in high] == [bug["created"]["uid"]]
    # The filter narrows, never widens: a plain-task lens leaves the register out.
    plain = _call(tasks_query_handler, db_session, kind="task")["tasks"]
    assert "Render drops the marker" not in {t["title"] for t in plain}


@pytest.mark.anyio
async def test_update_kind_and_severity_records_field_events(db_session, ledger):
    from app.integrations.tasks.models import TaskEvent
    from app.integrations.tasks.tools import tasks_update_handler

    out = _call(tasks_update_handler, db_session, uid="TASK-0001", kind="bug", severity="critical")
    assert out["updated"]["kind"] == "bug"
    assert out["updated"]["severity"] == "critical"
    events = {
        e.field: (e.old_value, e.new_value)
        for e in db_session.query(TaskEvent).filter(TaskEvent.field.in_(("kind", "severity")))
    }
    assert events == {"kind": ("task", "bug"), "severity": (None, "critical")}

    # Clearing severity is an explicit null, and is recorded like any other edit.
    out = _call(tasks_update_handler, db_session, uid="TASK-0001", severity=None)
    assert out["updated"]["severity"] is None
    cleared = (
        db_session.query(TaskEvent)
        .filter(TaskEvent.field == "severity", TaskEvent.new_value.is_(None))
        .one()
    )
    assert cleared.old_value == "critical"


@pytest.mark.anyio
async def test_unknown_kind_or_severity_is_refused_before_anything_is_written(db_session, ledger):
    from app.integrations.tasks.tools import (
        tasks_add_handler, tasks_bulk_update_handler, tasks_query_handler, tasks_update_handler,
    )

    with pytest.raises(ValueError, match="kind must be one of"):
        _call(tasks_add_handler, db_session, title="x", kind="defect")
    with pytest.raises(ValueError, match="severity must be one of"):
        _call(tasks_add_handler, db_session, title="x", kind="bug", severity="blocker")
    with pytest.raises(ValueError, match="kind must be one of"):
        _call(tasks_update_handler, db_session, uid="TASK-0001", kind="epic")
    with pytest.raises(ValueError, match="kind must be one of"):
        _call(tasks_query_handler, db_session, kind="epic")

    # In a bulk sweep the bad value fails ITS row and the good row still lands —
    # which is only true because the check runs before the CHECK constraint.
    out = _call(tasks_bulk_update_handler, db_session, updates=[
        {"uid": "TASK-0001", "kind": "chore"},
        {"uid": "TASK-0002", "kind": "nonsense"},
    ])
    assert out["updated"] == ["TASK-0001"]
    assert out["failed"][0]["uid"] == "TASK-0002"
    rows = {t["uid"]: t for t in _call(tasks_query_handler, db_session, owner="household")["tasks"]}
    assert rows["TASK-0001"]["kind"] == "chore"
    assert rows["TASK-0002"]["kind"] == "task"


@pytest.mark.anyio
async def test_split_halves_inherit_kind_and_severity(db_session, ledger):
    from app.integrations.tasks.tools import tasks_split_handler, tasks_update_handler

    _call(tasks_update_handler, db_session, uid="TASK-0001", kind="bug", severity="medium")
    out = _call(tasks_split_handler, db_session, uid="TASK-0001", parts=["Find the cause", "Fix it"])
    assert out["kept"]["kind"] == "bug"
    assert [(t["kind"], t["severity"]) for t in out["created"]] == [("bug", "medium")]


# ------------------------------------------------- one-way render, unconditional
#
# lios#202 (2026-08): nine `tasks_add` calls in a row each returned a bare
# `{"error": "... has been edited since it was last rendered ..."}` while the
# row had actually been created in every case — a render-guard trip after
# the DB write had already committed, surfacing as a top-level failure
# indistinguishable from "nothing happened". #202's fix made that failure
# non-fatal (`render_skipped`/`render_error` folded into an otherwise-normal
# response). lios#231/2026-09-14: the guard itself was removed — one stray
# hand edit caused six `tasks_add` calls in a row to report
# `render_skipped: true`, which is the ledger and the file drifting apart,
# exactly the failure the guard existed to prevent. The render is now
# unconditional: it always overwrites, so there is nothing left to skip and
# `render_skipped`/`render_error` no longer appear in any tool's response.


def _hand_edit(note):
    """Simulate someone opening the generated file and typing into it."""
    note.write_text(note.read_text() + "\nhand edit\n")


@pytest.mark.anyio
async def test_tasks_add_overwrites_a_hand_edited_file_unconditionally(db_session, ledger):
    """The render never refuses any more — see the module note above. A
    file that was hand-edited since the last render is simply overwritten,
    the DB write commits as normal, and nothing in the response reports a
    skip (because there is no longer a concept of one)."""
    from app.integrations.tasks.tools import tasks_add_handler

    _call(tasks_add_handler, db_session, title="Book the dentist")
    _hand_edit(ledger)

    out = _call(tasks_add_handler, db_session, title="Renew the passport")

    assert "error" not in out
    assert "render_skipped" not in out
    assert "render_error" not in out
    assert out["created"]["title"] == "Renew the passport"
    uid = out["created"]["uid"]

    from app.integrations.tasks.models import Task
    assert db_session.query(Task).filter(Task.uid == uid).one().title == "Renew the passport"

    # The hand edit is gone — the render is write-only and unconditional.
    assert "hand edit" not in ledger.read_text()
    assert "Renew the passport" in ledger.read_text()


@pytest.mark.anyio
async def test_tasks_update_also_overwrites_unconditionally(db_session, ledger):
    """Same shape, a different write-then-render tool — the render lives in
    the shared `_render` helper, not duplicated per tool."""
    from app.integrations.tasks.tools import tasks_add_handler, tasks_update_handler

    _call(tasks_add_handler, db_session, title="Book the dentist")
    _hand_edit(ledger)

    out = _call(tasks_update_handler, db_session, uid="TASK-0001", priority="high")

    assert "error" not in out
    assert "render_skipped" not in out
    assert out["updated"]["priority"] == "high"
    assert "hand edit" not in ledger.read_text()


@pytest.mark.anyio
async def test_tasks_bulk_update_also_overwrites_unconditionally(db_session, ledger):
    from app.integrations.tasks.tools import tasks_add_handler, tasks_bulk_update_handler

    _call(tasks_add_handler, db_session, title="Book the dentist")
    _hand_edit(ledger)

    out = _call(
        tasks_bulk_update_handler, db_session,
        updates=[{"uid": "TASK-0001", "priority": "low"}],
    )

    assert "error" not in out
    assert "render_skipped" not in out
    assert out["updated"] == ["TASK-0001"]
    assert "hand edit" not in ledger.read_text()


# ── lios#224: source vocabulary + confirmed_at gate ─────────────────────────


@pytest.mark.anyio
async def test_add_defaults_source_to_manual(db_session, ledger):
    from app.integrations.tasks.tools import tasks_add_handler

    out = _call(tasks_add_handler, db_session, title="Book the dentist")

    assert out["created"]["source"] == "manual"


@pytest.mark.anyio
async def test_add_accepts_every_vocabulary_value(db_session, ledger):
    from app.integrations.tasks.models import TASK_SOURCES
    from app.integrations.tasks.tools import tasks_add_handler

    for source in TASK_SOURCES:
        out = _call(tasks_add_handler, db_session, title=f"Task for {source}", source=source)
        assert out["created"]["source"] == source


@pytest.mark.anyio
async def test_add_rejects_an_unknown_source(db_session, ledger):
    """The live crash this closes: `source="meeting:2026-09-13 Household
    Systems & Weekly Check-in"` (55+ chars) used to reach the DB and raise
    `psycopg2.errors.StringDataRightTruncation` against the String(30)
    column. Now it is refused before it gets there, with a clear error."""
    from app.integrations.tasks.tools import tasks_add_handler

    with pytest.raises(ValueError, match="source must be one of"):
        _call(
            tasks_add_handler, db_session, title="x",
            source="meeting:2026-09-13 Household Systems & Weekly Check-in",
        )


@pytest.mark.anyio
async def test_add_defaults_confirmed_true_and_is_active_immediately(db_session, ledger):
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    out = _call(tasks_add_handler, db_session, title="Book the dentist")
    uid = out["created"]["uid"]
    assert out["created"]["confirmed_at"] is not None

    listed = _call(tasks_query_handler, db_session)
    assert uid in {t["uid"] for t in listed["tasks"]}


@pytest.mark.anyio
async def test_confirmed_false_is_excluded_from_the_default_query(db_session, ledger):
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    out = _call(tasks_add_handler, db_session, title="A suggested task", confirmed=False)
    uid = out["created"]["uid"]
    assert out["created"]["confirmed_at"] is None

    default = _call(tasks_query_handler, db_session)
    assert uid not in {t["uid"] for t in default["tasks"]}

    included = _call(tasks_query_handler, db_session, include_unconfirmed=True)
    assert uid in {t["uid"] for t in included["tasks"]}

    only = _call(tasks_query_handler, db_session, unconfirmed_only=True)
    assert {t["uid"] for t in only["tasks"]} == {uid}


@pytest.mark.anyio
async def test_confirmed_false_is_excluded_from_tasks_review(db_session, ledger):
    """Mutation-checked by hand for this PR: removing the
    `Task.confirmed_at.isnot(None)` filter from `tasks_review_handler` makes
    this fail (the flagged title shows up in `findings`), confirming the
    test actually exercises the gate."""
    from app.integrations.tasks.tools import tasks_add_handler, tasks_review_handler

    _call(
        tasks_add_handler, db_session, confirmed=False,
        title="Container name not a next action",  # matches review.py's flag heuristics
    )

    out = _call(tasks_review_handler, db_session, owner="household")
    assert "Container name not a next action" not in [f["title"] for f in out["findings"]]


@pytest.mark.anyio
async def test_tasks_confirm_makes_a_suggestion_active(db_session, ledger):
    from app.integrations.tasks.tools import (
        tasks_add_handler, tasks_confirm_handler, tasks_query_handler,
    )

    out = _call(tasks_add_handler, db_session, title="A suggested task", confirmed=False)
    uid = out["created"]["uid"]

    confirmed = _call(tasks_confirm_handler, db_session, uid=uid)
    assert confirmed["confirmed"]["confirmed_at"] is not None
    assert confirmed["already_confirmed"] is False

    default = _call(tasks_query_handler, db_session)
    assert uid in {t["uid"] for t in default["tasks"]}

    # A second confirm is a no-op, not an error.
    again = _call(tasks_confirm_handler, db_session, uid=uid)
    assert again["already_confirmed"] is True


@pytest.mark.anyio
async def test_tasks_decline_dismisses_an_unconfirmed_task(db_session, ledger):
    from app.integrations.tasks.tools import (
        tasks_add_handler, tasks_decline_handler, tasks_query_handler,
    )

    out = _call(tasks_add_handler, db_session, title="A suggested task", confirmed=False)
    uid = out["created"]["uid"]

    declined = _call(tasks_decline_handler, db_session, uid=uid, note="not this")
    assert declined["declined"]["status"] == "dropped"

    default = _call(tasks_query_handler, db_session, include_unconfirmed=True)
    assert uid not in {t["uid"] for t in default["tasks"]}


@pytest.mark.anyio
async def test_tasks_decline_still_requires_pending_owner_for_a_confirmed_task(db_session, ledger):
    """The dismiss branch must never swallow the ordinary transfer-decline
    refusal for an ordinary (confirmed) task with no pending transfer."""
    from app.integrations.tasks.tools import tasks_add_handler, tasks_decline_handler

    out = _call(tasks_add_handler, db_session, title="An ordinary task")
    uid = out["created"]["uid"]

    with pytest.raises(ValueError, match="has no transfer pending"):
        _call(tasks_decline_handler, db_session, uid=uid)
