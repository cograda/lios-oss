"""The task ledger's write path.

Every write re-renders `Task Backlog.md`. That is not decoration: from the
moment the file became a view, a change that does not re-render is a change
nobody can see.
"""

import json

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
    from app.integrations.tasks.importer import import_backlog

    note = tmp_path / "Task Backlog.md"
    note.write_text("placeholder\n")
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    import_backlog(db_session, SAMPLE)
    return note


def _call(handler, session, **args):
    return json.loads(handler(session, args))


@pytest.mark.anyio
async def test_query_returns_open_tasks_by_default(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler

    out = _call(tasks_query_handler, db_session)

    assert out["count"] == 2
    assert {t["uid"] for t in out["tasks"]} == {"TASK-0001", "TASK-0002"}
    assert out["tasks"][0]["project"] == "Malahide House"


@pytest.mark.anyio
async def test_query_filters_the_way_the_file_s_lenses_do(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler

    assert _call(tasks_query_handler, db_session, priority="highest")["count"] == 1
    assert _call(tasks_query_handler, db_session, context="errand")["count"] == 1
    assert _call(tasks_query_handler, db_session, energy="quick")["count"] == 1
    assert _call(tasks_query_handler, db_session, tag="#home")["count"] == 2
    assert _call(tasks_query_handler, db_session, text="seed")["count"] == 1


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
