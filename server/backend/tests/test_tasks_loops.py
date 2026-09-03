"""The loops layer: domains → programs → projects → tasks, definitions of done,
explicit queues, containment, and field-level history.

Decided 2026-09-01. The rule under test everywhere here: **a loop is anything
with a definition of done, and it cannot close while a sub-loop is open.**
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
- [ ] **Book the boiler service** #home
"""


@pytest.fixture
def ledger(db_session, tmp_path, monkeypatch):
    from app.integrations.tasks.importer import import_backlog

    note = tmp_path / "Task Backlog.md"
    note.write_text("placeholder\n")
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    import_backlog(db_session, SAMPLE)
    return note


@pytest.fixture
def domains(db_session):
    """Two household domains. Created the way the household integration does,
    because the tasks layer must never create one."""
    from app.integrations.household.models import Domain
    from app.models import User

    user = db_session.query(User).first()
    if user is None:
        user = User(email="alex@example.test", name="Alex")
        db_session.add(user)
        db_session.flush()
    for name in ("Home", "Finance"):
        db_session.add(Domain(
            name=name, owner_id=user.id,
            operational_definition=f"{name} things", scope_note="test",
        ))
    db_session.commit()
    return ["Home", "Finance"]


def _call(handler, session, **args):
    return json.loads(handler(session, args))


# ─── programs and projects ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_program_may_have_no_definition_of_done_but_a_project_must(db_session, ledger):
    from app.integrations.tasks.loops import tasks_program_handler, tasks_project_handler

    out = _call(tasks_program_handler, db_session, title="House", note_path="Projects/House.md")
    assert out["add"]["uid"] == "PROG-0001"
    assert out["add"]["done_when"] is None

    with pytest.raises(ValueError, match="definition of done"):
        _call(tasks_project_handler, db_session, title="Kitchen", program="House")

    out = _call(
        tasks_project_handler, db_session,
        title="Kitchen", program="House", done_when="Kitchen fitted and signed off",
    )
    assert out["add"]["done_when"] == "Kitchen fitted and signed off"


@pytest.mark.anyio
async def test_structure_shows_the_hierarchy_with_open_counts(db_session, ledger):
    from app.integrations.tasks.loops import (
        tasks_program_handler, tasks_project_handler, tasks_structure_handler,
    )

    _call(tasks_program_handler, db_session, title="House", projects=["Malahide House"])
    _call(tasks_project_handler, db_session, title="Loose", done_when="x")

    out = _call(tasks_structure_handler, db_session)
    house = out["programs"][0]
    assert house["title"] == "House"
    assert [p["title"] for p in house["projects"]] == ["Malahide House"]
    assert house["open_tasks"] == 3
    assert [p["title"] for p in out["projects_without_program"]] == ["Loose"]


@pytest.mark.anyio
async def test_a_project_cannot_close_over_an_open_task_and_a_program_over_an_open_project(db_session, ledger):
    from app.integrations.tasks.loops import tasks_program_handler, tasks_project_handler
    from app.integrations.tasks.tools import tasks_bulk_update_handler

    _call(tasks_program_handler, db_session, title="House", projects=["Malahide House"])

    with pytest.raises(ValueError, match="open tasks"):
        _call(tasks_project_handler, db_session, action="update", project="Malahide House", status="complete")
    with pytest.raises(ValueError, match="open projects"):
        _call(tasks_program_handler, db_session, action="update", program="House", status="complete")

    _call(tasks_bulk_update_handler, db_session, updates=[
        {"uid": u, "status": "done"} for u in ("TASK-0001", "TASK-0002", "TASK-0003")
    ])
    out = _call(tasks_project_handler, db_session, action="update", project="Malahide House", status="complete")
    assert out["update"]["status"] == "complete"
    out = _call(tasks_program_handler, db_session, action="update", program="House", status="complete")
    assert out["update"]["status"] == "complete"


# ─── domains: many-to-many tags ────────────────────────────────────────────


@pytest.mark.anyio
async def test_domains_are_tags_on_any_level_and_a_task_inherits_them(db_session, ledger, domains):
    from app.integrations.tasks.loops import tasks_program_handler, tasks_project_handler
    from app.integrations.tasks.tools import tasks_query_handler, tasks_update_handler

    _call(tasks_program_handler, db_session, title="House", domains=["Home", "Finance"], projects=["Malahide House"])
    _call(tasks_project_handler, db_session, action="update", project="Malahide House", domains=["Home"])
    _call(tasks_update_handler, db_session, uid="TASK-0001", domains=["Finance"])

    rows = {t["uid"]: t for t in _call(tasks_query_handler, db_session)["tasks"]}
    # Own tag first, then inherited, deduplicated — a program tagged both is
    # not a task tagged twice.
    assert rows["TASK-0001"]["domains"] == ["Finance", "Home"]
    assert rows["TASK-0002"]["domains"] == ["Home", "Finance"]

    assert _call(tasks_query_handler, db_session, domain="finance")["count"] == 3


@pytest.mark.anyio
async def test_an_unknown_domain_is_refused_with_the_ones_that_exist(db_session, ledger, domains):
    from app.integrations.tasks.tools import tasks_update_handler

    with pytest.raises(ValueError, match="Unknown domain.*Garden.*Existing domains: Finance, Home"):
        _call(tasks_update_handler, db_session, uid="TASK-0001", domains=["Garden"])


# ─── queues ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_focus_is_a_subset_of_week_and_null_clears_both(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler, tasks_update_handler

    _call(tasks_update_handler, db_session, uid="TASK-0001", queue="focus")
    _call(tasks_update_handler, db_session, uid="TASK-0002", queue="week")

    week = {t["uid"] for t in _call(tasks_query_handler, db_session, queue="week")["tasks"]}
    focus = {t["uid"] for t in _call(tasks_query_handler, db_session, queue="focus")["tasks"]}
    assert week == {"TASK-0001", "TASK-0002"}
    assert focus == {"TASK-0001"}

    out = _call(tasks_update_handler, db_session, uid="TASK-0001", queue=None)
    assert out["updated"]["queue"] is None
    assert out["updated"]["queue_set_at"] is None
    assert _call(tasks_query_handler, db_session, queue="week")["count"] == 1

    with pytest.raises(ValueError, match="queue must be"):
        _call(tasks_update_handler, db_session, uid="TASK-0001", queue="someday")


@pytest.mark.anyio
async def test_the_file_s_lenses_read_the_queue(db_session, ledger):
    from app.integrations.tasks.tools import tasks_update_handler

    _call(tasks_update_handler, db_session, uid="TASK-0001", queue="focus")
    _call(tasks_update_handler, db_session, uid="TASK-0002", queue="week")
    text = ledger.read_text()
    tv = next(line for line in text.splitlines() if "Wall mount the TV" in line)
    seed = next(line for line in text.splitlines() if "Collect the seed" in line)
    assert "#week" in tv and "#focus" in tv
    assert "#week" in seed and "#focus" not in seed
    assert "tag includes #week" in text  # the lens itself
    assert tv.count("#focus") == 1


# ─── containment ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_task_cannot_complete_while_a_sub_task_is_open(db_session, ledger):
    from app.integrations.tasks.tools import tasks_complete_handler, tasks_update_handler

    _call(tasks_update_handler, db_session, uid="TASK-0002", parent="TASK-0001")

    with pytest.raises(ValueError, match="open sub-tasks.*TASK-0002"):
        _call(tasks_complete_handler, db_session, uid="TASK-0001")

    _call(tasks_complete_handler, db_session, uid="TASK-0002")
    out = _call(tasks_complete_handler, db_session, uid="TASK-0001")
    assert out["completed"]["status"] == "done"


@pytest.mark.anyio
async def test_containment_refuses_cycles_and_self(db_session, ledger):
    from app.integrations.tasks.tools import tasks_update_handler

    _call(tasks_update_handler, db_session, uid="TASK-0002", parent="TASK-0001")
    _call(tasks_update_handler, db_session, uid="TASK-0003", parent="TASK-0002")
    with pytest.raises(ValueError, match="cycle"):
        _call(tasks_update_handler, db_session, uid="TASK-0001", parent="TASK-0003")
    with pytest.raises(ValueError, match="part of itself"):
        _call(tasks_update_handler, db_session, uid="TASK-0001", parent="TASK-0001")


# ─── history ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_every_field_change_is_an_event_and_history_separates_done_from_tidied(db_session, ledger):
    from app.integrations.tasks.loops import tasks_history_handler
    from app.integrations.tasks.tools import tasks_complete_handler, tasks_update_handler

    _call(tasks_update_handler, db_session, uid="TASK-0001", priority="low", queue="week")
    _call(tasks_update_handler, db_session, uid="TASK-0001", priority="low")  # no-op: no event
    _call(tasks_complete_handler, db_session, uid="TASK-0002")

    out = _call(tasks_history_handler, db_session)
    fields = [(e["uid"], e["field"], e["from"], e["to"]) for e in out["events"]]
    assert ("TASK-0001", "priority", "highest", "low") in fields
    assert ("TASK-0001", "queue", None, "week") in fields
    assert ("TASK-0002", "status", "next", "done") in fields
    assert out["completed"] == 1
    assert out["tidied"] == 2
