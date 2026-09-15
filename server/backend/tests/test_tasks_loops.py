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
    from tests.legacy_backlog_importer import import_backlog

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

    # The imported sample tasks have no owner, so the household view is the
    # one that counts them; the caller-scoped default is tested separately.
    out = _call(tasks_structure_handler, db_session, scope="household")
    house = out["programs"][0]
    assert house["title"] == "House"
    assert [p["title"] for p in house["projects"]] == ["Malahide House"]
    assert house["open_tasks"] == 3
    assert [p["title"] for p in out["projects_without_program"]] == ["Loose"]


@pytest.mark.anyio
async def test_structure_defaults_to_the_callers_own_hierarchy(db_session, ledger):
    """2026-09-06: the Loops rail showed Sam all of Alex's programs with his
    counts. Default scope is the caller: what they own, plus any project that
    holds one of their open loops, counted for them alone."""
    from app.auth.context import use_user
    from app.integrations.tasks.loops import (
        tasks_program_handler, tasks_project_handler, tasks_structure_handler,
    )
    from app.integrations.tasks.tools import tasks_add_handler

    # user 1 (the ledger fixture's caller) owns House → Malahide House, and a loose project.
    _call(tasks_program_handler, db_session, title="House", projects=["Malahide House"])
    _call(tasks_project_handler, db_session, title="Loose", done_when="x")
    with use_user(2):
        _call(tasks_project_handler, db_session, title="Sam's own", done_when="y")
        _call(tasks_add_handler, db_session, title="Paint the shed door", project="Malahide House")
        _call(tasks_add_handler, db_session, title="Order bobbins")  # no project

        mine = _call(tasks_structure_handler, db_session)
    # Sam sees House only because one of HER loops is filed under it — with her count, not 3.
    assert [p["title"] for p in mine["programs"]] == ["House"]
    assert mine["programs"][0]["open_tasks"] == 1
    assert [p["title"] for p in mine["programs"][0]["projects"]] == ["Malahide House"]
    # Her own project appears; Alex's loose project does not.
    assert [p["title"] for p in mine["projects_without_program"]] == ["Sam's own"]
    assert mine["tasks_without_project"] == 1

    # Alex's default view: his projects, none of her counts.
    alex = _call(tasks_structure_handler, db_session)
    assert [p["title"] for p in alex["projects_without_program"]] == ["Loose"]
    assert alex["programs"][0]["open_tasks"] == 0  # the imported tasks are unowned
    assert alex["tasks_without_project"] == 0

    # Household scope sees everything and everyone's counts.
    everyone = _call(tasks_structure_handler, db_session, scope="household")
    assert everyone["programs"][0]["open_tasks"] == 4
    assert {p["title"] for p in everyone["projects_without_program"]} == {"Loose", "Sam's own"}
    with pytest.raises(ValueError):
        _call(tasks_structure_handler, db_session, scope="everyone")


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

    rows = {t["uid"]: t for t in _call(tasks_query_handler, db_session, owner="household")["tasks"]}
    # Own tag first, then inherited, deduplicated — a program tagged both is
    # not a task tagged twice.
    assert rows["TASK-0001"]["domains"] == ["Finance", "Home"]
    assert rows["TASK-0002"]["domains"] == ["Home", "Finance"]

    assert _call(tasks_query_handler, db_session, owner="household", domain="finance")["count"] == 3


@pytest.mark.anyio
async def test_an_unknown_domain_is_refused_with_the_ones_that_exist(db_session, ledger, domains):
    from app.integrations.tasks.tools import tasks_update_handler

    with pytest.raises(ValueError, match="Unknown domain.*Garden.*Existing domains: Finance, Home"):
        _call(tasks_update_handler, db_session, uid="TASK-0001", domains=["Garden"])


# ─── domains: a free tag naming a domain IS that domain ────────────────────
#
# Found 2026-09-07: 65 open tasks carried `#admin`, 32 `#home`, 31 `#kids`,
# and none of them counted as in that domain, because only an explicit
# `task_domain_tags` row ever did. Resolution happens in `domain_names()`
# and nowhere else, so the filter, the row, the structure counts and the
# routines listing all agree.


def _add_domain(session, name):
    from app.integrations.household.models import Domain
    from app.models import User

    user = session.query(User).first()
    session.add(Domain(name=name, owner_id=user.id, operational_definition=name, scope_note="test"))
    session.commit()


@pytest.mark.anyio
async def test_a_free_tag_naming_a_domain_counts_as_that_domain_everywhere(db_session, ledger, domains):
    from app.integrations.tasks.loops import tasks_structure_handler
    from app.integrations.tasks.models import TaskDomainTag
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    _add_domain(db_session, "Admin")
    made = _call(tasks_add_handler, db_session, title="Renew the passport", tags=["#admin"])
    shouted = _call(tasks_add_handler, db_session, title="File the return", tags=["#ADMIN", "#quick"])
    uid, uid2 = made["created"]["uid"], shouted["created"]["uid"]
    # Resolution, not migration: no row was written.
    assert db_session.query(TaskDomainTag).count() == 0

    # Serialised on the row, case-insensitively, with the domain's own casing.
    assert made["created"]["domains"] == ["Admin"]
    rows = {t["uid"]: t for t in _call(tasks_query_handler, db_session, owner="household")["tasks"]}
    assert rows[uid2]["domains"] == ["Admin"]
    # The domain= filter finds both, and only them.
    found = {t["uid"] for t in _call(tasks_query_handler, db_session, owner="household", domain="admin")["tasks"]}
    assert found == {uid, uid2}
    # And the structure counts them.
    out = _call(tasks_structure_handler, db_session, scope="household")
    assert out["domain_counts"]["Admin"] == 2
    # The imported sample lines all carry `#home`, so Home now counts them too.
    assert out["domain_counts"]["Home"] == 3
    assert out["domain_counts"]["Finance"] == 0


@pytest.mark.anyio
async def test_a_tag_matching_no_domain_resolves_to_nothing(db_session, ledger, domains):
    from app.integrations.tasks.loops import tasks_structure_handler
    from app.integrations.tasks.tools import tasks_add_handler, tasks_query_handler

    made = _call(tasks_add_handler, db_session, title="Prune the apple tree", tags=["#garden", "#person/isla"])
    assert made["created"]["domains"] == []
    assert _call(tasks_query_handler, db_session, owner="household", domain="garden")["count"] == 0
    # A namespaced tag never matches a domain even if the domain shares the
    # first segment: `#home/office` is not `#home`.
    out = _call(tasks_add_handler, db_session, title="Tidy the desk", tags=["#home/office"])
    assert out["created"]["domains"] == []
    counts = _call(tasks_structure_handler, db_session, scope="household")["domain_counts"]
    assert set(counts) == {"Finance", "Home"}


@pytest.mark.anyio
async def test_explicit_row_and_matching_tag_on_one_task_yield_one_name(db_session, ledger, domains):
    from app.integrations.tasks.loops import tasks_structure_handler
    from app.integrations.tasks.tools import tasks_query_handler, tasks_update_handler

    # TASK-0001 already carries `#home`; now also tag it Home explicitly.
    out = _call(tasks_update_handler, db_session, uid="TASK-0001", domains=["Home"])
    assert out["updated"]["domains"] == ["Home"]
    # Asserted at the resolution point itself, not only on the row: `_rows`
    # deduplicates again downstream, so a duplicate here would hide there —
    # and surface in the routines listing and the `domains` field event.
    from app.integrations.tasks.loops import domain_names
    from app.integrations.tasks.models import Task
    task_id = db_session.query(Task.id).filter(Task.uid == "TASK-0001").scalar()
    assert domain_names(db_session)[("task", task_id)] == ["Home"]
    assert _call(tasks_query_handler, db_session, owner="household", domain="Home")["count"] == 3
    assert _call(tasks_structure_handler, db_session, scope="household")["domain_counts"]["Home"] == 3
    # Explicit first, then tag-derived: Finance by row, Home by tag.
    out = _call(tasks_update_handler, db_session, uid="TASK-0001", domains=["Finance"])
    assert out["updated"]["domains"] == ["Finance", "Home"]


@pytest.fixture
def eight_domains(db_session):
    """The 8 household domains as they exist in production, measured via
    `household_domains_list` on 2026-09-08 (Admin, Health, Home, Kids, Money,
    Social, Tech, Work) — N6 (Domains as tags, populated). This is the
    test-tier proof that the wiring works at the real shape and count Alex's
    definitions will land in: `household_domain_add` populates `domains`,
    nothing here re-invents a definition, and every consumer downstream
    (tasks_structure, the free-tag resolution, the Loops BFF, the sidebar)
    is exercised against it. See `vault/Projects/lios/Plans/2026-09-08 Domain
    definitions — draft for Alex.md` for the production measurement this
    fixture mirrors."""
    from app.integrations.household.models import Domain
    from app.models import User

    user = db_session.query(User).first()
    if user is None:
        user = User(email="alex@example.test", name="Alex")
        db_session.add(user)
        db_session.flush()
    names = ["Admin", "Health", "Home", "Kids", "Money", "Social", "Tech", "Work"]
    for name in names:
        db_session.add(Domain(
            name=name, owner_id=user.id,
            operational_definition=f"{name} things", scope_note="test",
        ))
    db_session.commit()
    return names


@pytest.mark.anyio
async def test_eight_domains_populate_structure_and_resolve_via_tags(
    db_session, tmp_path, monkeypatch, eight_domains,
):
    """The end-to-end proof N6 asks for, at the real 8-domain shape: once
    `domains` rows exist, `tasks_structure` carries all of them (even the
    ones with zero open tasks — a domain with nothing open is a fact, not
    noise, per the rail's own contract) and a plain free tag on a task
    resolves to its domain with no explicit `task_domain_tags` row at all —
    this is exactly the path `apps/loops`'s `GET /api/structure` (a plain
    passthrough of this tool) and `StructureRail.tsx` render on top of."""
    from app.integrations.tasks.loops import domain_names, tasks_structure_handler
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_add_handler

    # Redirect the generated backlog-note write, same as `ledger` above, but
    # without importing its SAMPLE data — this test wants a clean ledger of
    # exactly the two tasks it creates, so the zero-count domains stay zero.
    note = tmp_path / "Task Backlog.md"
    note.write_text("placeholder\n")
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )

    made = _call(tasks_add_handler, db_session, title="Renew the passport", tags=["#admin"])
    made2 = _call(tasks_add_handler, db_session, title="Book Finn's assessment", tags=["#kids"])
    uid1, uid2 = made["created"]["uid"], made2["created"]["uid"]

    out = _call(tasks_structure_handler, db_session, scope="household")
    # All 8 domains present, alphabetically, regardless of open-task count.
    assert out["domains"] == sorted(eight_domains)
    assert set(out["domain_counts"]) == set(eight_domains)
    assert out["domain_counts"]["Admin"] == 1
    assert out["domain_counts"]["Kids"] == 1
    # Every other domain is present with an explicit zero, not omitted.
    for name in ("Health", "Home", "Money", "Social", "Tech", "Work"):
        assert out["domain_counts"][name] == 0

    # Resolution happens with no row written to task_domain_tags — a free
    # tag naming a domain IS that domain (domain_names() is the single
    # place every consumer, including tasks_structure above, agrees on it).
    task1_id = db_session.query(Task.id).filter(Task.uid == uid1).scalar()
    task2_id = db_session.query(Task.id).filter(Task.uid == uid2).scalar()
    assert domain_names(db_session)[("task", task1_id)] == ["Admin"]
    assert domain_names(db_session)[("task", task2_id)] == ["Kids"]


# ─── queues ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_focus_is_a_subset_of_week_and_null_clears_both(db_session, ledger):
    from app.integrations.tasks.tools import tasks_query_handler, tasks_update_handler

    _call(tasks_update_handler, db_session, uid="TASK-0001", queue="focus")
    _call(tasks_update_handler, db_session, uid="TASK-0002", queue="week")

    week = {t["uid"] for t in _call(tasks_query_handler, db_session, owner="household", queue="week")["tasks"]}
    focus = {t["uid"] for t in _call(tasks_query_handler, db_session, owner="household", queue="focus")["tasks"]}
    assert week == {"TASK-0001", "TASK-0002"}
    assert focus == {"TASK-0001"}

    out = _call(tasks_update_handler, db_session, uid="TASK-0001", queue=None)
    assert out["updated"]["queue"] is None
    assert out["updated"]["queue_set_at"] is None
    assert _call(tasks_query_handler, db_session, owner="household", queue="week")["count"] == 1

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

    # The imported SAMPLE rows are nobody's, so the caller-scoped default
    # feed (2026-09-06) would not carry them; ask for everyone's.
    out = _call(tasks_history_handler, db_session, owner="household")
    fields = [(e["uid"], e["field"], e["from"], e["to"]) for e in out["events"]]
    assert ("TASK-0001", "priority", "highest", "low") in fields
    assert ("TASK-0001", "queue", None, "week") in fields
    assert ("TASK-0002", "status", "next", "done") in fields
    assert out["completed"] == 1
    assert out["tidied"] == 2
