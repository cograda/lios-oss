"""Importing Task Backlog.md into the ledger.

The load-bearing test here is idempotency: the import will be run more than
once (a dry run, a fix, a real run), and a duplicate-on-rerun bug would be
discovered only by a person noticing their backlog had doubled.
"""

from datetime import timezone

import pytest

pytestmark = pytest.mark.db

BACKLOG = """---
title: Task Backlog
---

# Task Backlog

> The single unified backlog.

```tasks
not done
path includes Task Backlog.md
tag includes #focus
```

# Backlog — by category

# Home

## [[Malahide House]]

- [ ] **Wall mount the TV** 🔺 #home #quick
  - Bracket, drill, cable route.
  - Batch with the other wall jobs.
- [ ] **Collect the grass seed** ⏫ #home #errand 📅 2026-09-02
- [x] ~~**Stop the HomePod notifications**~~ 🔺 #home #quick ✅ 2026-08-30

## [[Home Sensors & Automation]]

- [ ] **Settle the camera capacity question with [[Sam Rivers]]** ⏫ #deep #sitdown

# Admin

- [ ] **Renew the car insurance** 🔼
"""


def _import(session, content=BACKLOG, **kw):
    from app.integrations.tasks.importer import import_backlog

    return import_backlog(session, content, **kw)


@pytest.mark.anyio
async def test_import_creates_the_stated_shape(db_session):
    from app.integrations.tasks.models import Task, TaskProject

    result = _import(db_session)

    assert result.parsed == 5
    assert result.tasks_created == 5
    # Two H2 sections; the Admin task sits under none and must not invent one.
    assert result.projects_created == 2

    # Titles keep the file's own notation: bold and [[wikilinks]] are content
    # a person typed, not metadata. Only priority/dates/tags are stripped,
    # because those become columns and would otherwise be stored twice.
    tasks = {t.title: t for t in db_session.query(Task).all()}
    assert set(tasks) == {
        "**Wall mount the TV**", "**Collect the grass seed**",
        "**Stop the HomePod notifications**",
        "**Settle the camera capacity question with [[Sam Rivers]]**",
        "**Renew the car insurance**",
    }

    tv = tasks["**Wall mount the TV**"]
    assert tv.status == "next"
    assert tv.priority == "highest"
    assert tv.energy == "quick"
    # Sub-bullets are the constraints and must survive as the description.
    assert "Bracket, drill" in tv.description
    assert "Batch with the other" in tv.description

    seed = tasks["**Collect the grass seed**"]
    assert seed.context == "errand"
    assert seed.due_at.date().isoformat() == "2026-09-02"

    camera = tasks["**Settle the camera capacity question with [[Sam Rivers]]**"]
    assert camera.context == "sitdown"
    assert camera.energy == "deep"

    admin = tasks["**Renew the car insurance**"]
    assert admin.project_id is None

    projects = {p.title: p for p in db_session.query(TaskProject).all()}
    assert set(projects) == {"Malahide House", "Home Sensors & Automation"}
    # The H2's [[wikilink]] is the join back to the vault note.
    assert projects["Malahide House"].note_path == "Malahide House"


@pytest.mark.anyio
async def test_created_at_is_never_invented(db_session):
    """The whole point of the aging story: unknown must stay unknown.

    Stamping the import date would make every imported task look as though it
    was created the day the ledger shipped, and every 'open longest' answer
    computed from it would be wrong while looking entirely plausible.
    """
    from app.integrations.tasks.models import Task

    _import(db_session)

    assert db_session.query(Task).count() == 5
    assert all(t.created_at is None for t in db_session.query(Task).all())


@pytest.mark.anyio
async def test_rerunning_updates_rather_than_duplicates(db_session):
    from app.integrations.tasks.models import Task, TaskLink, TaskProject

    first = _import(db_session)
    second = _import(db_session)

    assert first.tasks_created == 5
    assert second.tasks_created == 0
    assert second.tasks_updated == 5
    assert db_session.query(Task).count() == 5
    # Projects and links must not accumulate either.
    assert db_session.query(TaskProject).count() == 2
    assert db_session.query(TaskLink).count() == first.links_created

    # uids are stable across the re-run — they are the human-facing identity.
    uids = {t.uid for t in db_session.query(Task).all()}
    assert uids == {f"TASK-{n:04d}" for n in range(1, 6)}


@pytest.mark.anyio
async def test_an_edited_priority_is_picked_up_on_reimport(db_session):
    from app.integrations.tasks.models import Task

    _import(db_session)
    edited = BACKLOG.replace("**Renew the car insurance** 🔼", "**Renew the car insurance** 🔺")
    _import(db_session, edited)

    task = db_session.query(Task).filter(Task.title == "**Renew the car insurance**").one()
    assert task.priority == "highest"
    assert db_session.query(Task).count() == 5


@pytest.mark.anyio
async def test_done_item_carries_its_completion_date_and_an_event(db_session):
    from app.integrations.tasks.models import Task, TaskEvent

    _import(db_session)

    done = db_session.query(Task).filter(Task.status == "done").one()
    assert done.title == "**Stop the HomePod notifications**"
    assert done.completed_at.date().isoformat() == "2026-08-30"

    event = db_session.query(TaskEvent).filter(TaskEvent.task_id == done.id).one()
    assert event.to_status == "done"
    assert event.at.astimezone(timezone.utc).date().isoformat() == "2026-08-30"


@pytest.mark.anyio
async def test_category_and_wikilinks_become_links_not_guesses(db_session):
    from app.integrations.tasks.models import Task, TaskLink

    _import(db_session)

    camera = db_session.query(Task).filter(Task.context == "sitdown").one()
    links = db_session.query(TaskLink).filter(TaskLink.from_task_id == camera.id).all()

    assert ("domain", "Home") in {(l.target_type, l.target_ref) for l in links}
    assert ("note", "Sam Rivers") in {(l.target_type, l.target_ref) for l in links}
    # Nothing here was inferred, so nothing may claim to have been.
    assert all(l.derived_by == "deterministic" and l.confidence == 1.0 for l in links)


@pytest.mark.anyio
async def test_query_lens_blocks_are_not_imported_as_tasks(db_session):
    """```tasks blocks are views over the list, not entries in it."""
    from app.integrations.tasks.models import Task

    _import(db_session)

    titles = [t.title for t in db_session.query(Task).all()]
    assert not any("path includes" in t or "not done" == t for t in titles)


@pytest.mark.anyio
async def test_dry_run_writes_nothing(db_session):
    from app.integrations.tasks.models import Task

    result = _import(db_session, dry_run=True)

    assert result.tasks_created == 5
    assert db_session.query(Task).count() == 0


@pytest.mark.anyio
async def test_a_long_task_line_loses_nothing(db_session):
    """The file's task lines are a title *plus* prose, and 12 of the live 246
    exceed the column. Truncating drops real commitments, so the overflow moves
    into the description and every character survives somewhere."""
    from app.integrations.tasks.models import Task

    tail = (
        "Reply to Hazel Mulvaney and book the final valuation — her 28 Jul mail "
        "confirms a final valuation from then. " + ("Terms follow in detail. " * 12)
        + "AIB now needs the certification of works complete."
    )
    content = BACKLOG + f"\n- [ ] **{tail}** 🔺\n"

    _import(db_session, content)

    task = db_session.query(Task).filter(Task.title.like("%Reply to Hazel%")).one()
    assert len(task.title) <= 300
    assert not task.title.endswith("Term")          # never mid-word
    rejoined = task.title + " " + (task.description or "").replace("\n\n", " ")
    assert "AIB now needs the certification of works complete." in rejoined
    # Nothing lost, nothing added — modulo the bold markers the title keeps.
    assert rejoined.replace("**", "").split() == tail.split()


def test_split_title_prefers_a_sentence_boundary():
    from app.integrations.tasks.importer import split_title

    title, overflow = split_title("A" * 250 + ". " + "B" * 100)
    assert title == "A" * 250 + "."
    assert overflow == "B" * 100

    short, none = split_title("Short one")
    assert (short, none) == ("Short one", None)


@pytest.mark.anyio
async def test_metadata_quoted_in_code_is_not_this_task_s_metadata(db_session):
    """A date inside `backticks` is being discussed, not declared.

    The live line that found this reads: "the original stale `📅 2026-06-11` is
    deliberately dropped rather than carried forward as a 7-week-overdue
    date" — and the importer set that very date as the task's due date,
    resurrecting the thing the note records as deliberately dropped.
    """
    from app.integrations.tasks.models import Task

    content = BACKLOG + (
        "\n- [ ] **Knee rehab block** — the original stale `📅 2026-06-11` is "
        "deliberately dropped, and `#quick` is quoted here too 🔼 #medical\n"
    )
    _import(db_session, content)

    task = db_session.query(Task).filter(Task.title.like("%Knee rehab%")).one()
    assert task.due_at is None                  # the quoted date is not a due date
    assert task.tags == ["#medical"]            # the quoted tag is not a tag
    assert task.energy is None                  # ...and did not become a column
    assert task.priority == "medium"            # the real glyph still counts
    # The quoted text stays intact in the title, backticks and all.
    assert "`📅 2026-06-11`" in task.title
    assert "`#quick`" in task.title
