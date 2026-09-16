"""Rendering the ledger back into Task Backlog.md.

🔑 The point of these tests is the round-trip, not the formatting. The file is
the only copy of its own content — it is gitignored and Drive-synced — so the
render may not be pointed at it until importing and re-rendering is provably
lossless. Every test here is a thing an earlier version of the renderer
actually destroyed.
"""

import re

import pytest

pytestmark = pytest.mark.db

SAMPLE = """---
title: Task Backlog
---

# Task Backlog

# Backlog — by category

# Home

## [[Malahide House]]

- [ ] **Wall mount [[Sam Rivers]]'s monitor** 🔼 #home #quick
- [ ] **Collect the seed** ⏫ #home #errand 📅 2026-09-02
  - Ordered, collection only.
- [x] ~~**Stop the HomePod notifications**~~ 🔺 #home #home-automation #quick ✅ 2026-08-30

## [[Comar]] — Home Server & Dev

> **Role note:** this section holds only human-action items.

### Standing up the committee

- [ ] **Renew the domain** 🔽 #admin #person/isla
"""


def _roundtrip(session, content=SAMPLE):
    from datetime import date

    from tests.legacy_backlog_importer import import_backlog
    from app.integrations.tasks.render import render_backlog

    import_backlog(session, content)
    return render_backlog(session, today=date(2026, 8, 30), last_reviewed="2026-08-06")


@pytest.mark.anyio
async def test_every_task_line_survives_with_all_its_tags(db_session):
    """#quick used to vanish: it became the `energy` column and was then
    filtered out of the stored tag list. Deriving a column from a tag must not
    consume the tag."""
    out = _roundtrip(db_session)

    assert "#home #quick" in out
    assert "#home #home-automation #quick" in out
    assert "#admin #person/isla" in out
    assert len(re.findall(r"^- \[", out, re.M)) == 4


@pytest.mark.anyio
async def test_wikilinks_and_bold_survive_in_titles(db_session):
    """An earlier clean_title unwrapped both, which made the line
    unrenderable without guessing where they had been."""
    out = _roundtrip(db_session)

    assert "**Wall mount [[Sam Rivers]]'s monitor**" in out


@pytest.mark.anyio
async def test_a_heading_keeps_the_text_after_its_wikilink(db_session):
    """`## [[Comar]] — Home Server & Dev` lost four words when the renderer
    emitted only the link."""
    out = _roundtrip(db_session)

    assert "## [[Comar]] — Home Server & Dev" in out


@pytest.mark.anyio
async def test_structure_survives(db_session):
    out = _roundtrip(db_session)

    assert "### Standing up the committee" in out
    assert "> **Role note:** this section holds only human-action items." in out
    assert out.index("## [[Malahide House]]") < out.index("## [[Comar]]")


@pytest.mark.anyio
async def test_done_state_dates_and_subbullets_survive(db_session):
    out = _roundtrip(db_session)

    assert "- [x] ~~**Stop the HomePod notifications**~~" in out
    assert "✅ 2026-08-30" in out
    assert "📅 2026-09-02" in out
    assert "  - Ordered, collection only." in out


@pytest.mark.anyio
async def test_rendering_never_claims_the_backlog_was_reviewed(db_session):
    """last-reviewed is a property of a sweep, not of a render. Regenerating
    the file must not silently assert someone read it."""
    out = _roundtrip(db_session)

    assert "last-reviewed: 2026-08-06" in out
    assert "modified: 2026-08-30" in out


@pytest.mark.anyio
async def test_file_order_is_preserved_not_priority_order(db_session):
    """Rendering by priority would reshuffle 246 items into an unreviewable
    diff. The file's order is a person's ordering of their own work."""
    out = _roundtrip(db_session)

    assert out.index("Wall mount") < out.index("Collect the seed")
    assert out.index("Collect the seed") < out.index("Stop the HomePod")


@pytest.mark.anyio
async def test_last_reviewed_comes_from_a_sweep_not_from_rendering(db_session):
    """Regenerating the file is not a review. Only a sweep moves the date."""
    from datetime import date, datetime, timezone

    from tests.legacy_backlog_importer import import_backlog
    from app.integrations.tasks.models import TaskEvent
    from app.integrations.tasks.render import (
        last_reviewed_date, record_sweep, render_backlog,
    )

    import_backlog(db_session, SAMPLE)

    # Never swept: the field is empty rather than claiming today.
    assert last_reviewed_date(db_session) is None
    out = render_backlog(db_session, today=date(2026, 8, 30))
    assert "last-reviewed: \n" in out or "last-reviewed:\n" in out.replace(" \n", "\n")

    event = record_sweep(db_session, note="weekly")
    event.at = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    db_session.commit()

    assert last_reviewed_date(db_session) == "2026-08-24"
    out = render_backlog(
        db_session, today=date(2026, 8, 30),
        last_reviewed=last_reviewed_date(db_session),
    )
    assert "last-reviewed: 2026-08-24" in out
    assert "modified: 2026-08-30" in out

    # A sweep belongs to no single task; that is why task_id is nullable.
    assert db_session.query(TaskEvent).filter(TaskEvent.task_id.is_(None)).count() == 1


@pytest.mark.anyio
async def test_an_empty_ledger_cannot_blank_the_file(db_session, tmp_path, monkeypatch):
    """The unrecoverable failure: migrations applied, import never run, and a
    valid-but-empty render overwrites the only copy of 246 tasks. The file is
    gitignored and Drive-synced, so there is nothing to recover from."""
    from app.integrations.tasks import render as render_mod

    note = tmp_path / "Task Backlog.md"
    note.write_text("- [ ] **Something real**\n")
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )

    with pytest.raises(RuntimeError, match="refusing to render an empty ledger"):
        render_mod.write_backlog_note(db_session, user_id=1)

    assert note.read_text() == "- [ ] **Something real**\n"


@pytest.mark.anyio
async def test_a_populated_ledger_does_write(db_session, tmp_path, monkeypatch):
    from app.integrations.tasks import render as render_mod
    from tests.legacy_backlog_importer import import_backlog

    note = tmp_path / "Task Backlog.md"
    note.write_text("stale\n")
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    import_backlog(db_session, SAMPLE)

    render_mod.write_backlog_note(db_session, user_id=1)

    written = note.read_text()
    assert "**Wall mount [[Sam Rivers]]'s monitor**" in written
    assert "stale" not in written


@pytest.mark.anyio
async def test_no_task_can_be_missing_from_the_render(db_session):
    """A task with no category used to vanish: the renderer only emitted known
    categories, and `tasks_add` creates no category link. It was written to the
    ledger, reported as created, and invisible in the file.

    This asserts the invariant rather than the symptom — every task in the
    ledger appears in the output, whatever its category.
    """
    import re
    from datetime import datetime, timezone

    from tests.legacy_backlog_importer import import_backlog
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.render import render_backlog

    import_backlog(db_session, SAMPLE)
    db_session.add(Task(
        uid="TASK-9001", title="**Orphan with no category**", status="next",
        confirmed_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    out = render_backlog(db_session)

    assert len(re.findall(r"^- \[", out, re.M)) == db_session.query(Task).count()
    assert "**Orphan with no category**" in out
    assert "# Uncategorised" in out


# --------------------------------------------------------- one-way, write-only
#
# lios#231 (2026-09-14): the render used to detect an external edit since the
# last render and refuse to regenerate unless `force=True`. One stray hand
# edit caused six `tasks_add` calls in a row to report `render_skipped: true`
# — the ledger and the file drifting apart, exactly the failure the guard
# was meant to prevent. The guard (edit detection, `check_drift`,
# `force`, the per-render digest ledger) is gone: every render overwrites
# the file unconditionally, and the file's own banner says so.

def _at(tmp_path, monkeypatch):
    note = tmp_path / "Task Backlog.md"
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    return note


@pytest.mark.anyio
async def test_a_hand_edit_is_overwritten_not_refused(db_session, tmp_path, monkeypatch):
    """The old failure mode this replaces: a render used to refuse outright
    once the file had been hand-edited, which is exactly what caused the
    2026-09-14 outage (six `tasks_add` calls in a row reporting a skipped
    render). Now the write is unconditional — the render proceeds, and the
    hand edit is discarded, every time, with no exception and no flag in the
    response."""
    from app.integrations.tasks import render as render_mod
    from tests.legacy_backlog_importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    render_mod.write_backlog_note(db_session, user_id=1)

    note.write_text(note.read_text() + "\n- [x] typed straight into the file\n")

    render_mod.write_backlog_note(db_session, user_id=1)  # must not raise
    fresh = note.read_text()
    assert "typed straight into the file" not in fresh
    assert "**Wall mount [[Sam Rivers]]'s monitor**" in fresh


@pytest.mark.anyio
async def test_junk_written_over_the_file_is_replaced_by_the_next_render(
    db_session, tmp_path, monkeypatch,
):
    """The mutation-guarding proof: write garbage that shares nothing with a
    real render, then render, and assert what lands is the actual fresh
    render — not a merge, not the garbage, not a no-op."""
    from app.integrations.tasks import render as render_mod
    from tests.legacy_backlog_importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    note.write_text("this is not a rendered backlog at all\n" * 5)

    render_mod.write_backlog_note(db_session, user_id=1)

    written = note.read_text()
    assert "this is not a rendered backlog at all" not in written
    assert "**Wall mount [[Sam Rivers]]'s monitor**" in written
    assert "**Collect the seed**" in written


@pytest.mark.anyio
async def test_writing_over_a_pre_existing_unrelated_file_succeeds(
    db_session, tmp_path, monkeypatch,
):
    """A file that was never one of our renders (e.g. right after a
    database restore, or the very first render on a machine) must not block
    anything — there is no baseline to compare against any more, and there
    doesn't need to be."""
    from app.integrations.tasks import render as render_mod
    from tests.legacy_backlog_importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    note.write_text("pre-existing, and not ours\n")
    import_backlog(db_session, SAMPLE)

    render_mod.write_backlog_note(db_session, user_id=1)  # must not raise
    assert "pre-existing, and not ours" not in note.read_text()


@pytest.mark.anyio
async def test_an_empty_file_can_be_written_over(db_session, tmp_path, monkeypatch):
    """2026-09-02 17:29:11: the host disk hit 100% mid-render and the file
    was left at 0 bytes. Under the old drift guard that read as a hand edit
    and every subsequent write was refused to protect nothing. There is no
    such guard now — an empty file is just overwritten like any other."""
    from app.integrations.tasks import render as render_mod
    from tests.legacy_backlog_importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    render_mod.write_backlog_note(db_session, user_id=1)
    note.write_text("")

    render_mod.write_backlog_note(db_session, user_id=1)  # must not raise
    assert note.stat().st_size > 0


@pytest.mark.anyio
async def test_the_banner_is_present_and_leads_the_body(db_session, tmp_path, monkeypatch):
    """The file has to say, in the file itself, that editing it is pointless
    — the banner is the one thing left standing in for the removed guard."""
    from app.integrations.tasks import render as render_mod
    from tests.legacy_backlog_importer import import_backlog

    _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    render_mod.write_backlog_note(db_session, user_id=1)

    out = render_mod.write_backlog_note(db_session, user_id=1)
    assert out == "Task Backlog.md"
    from app.integrations.tasks.render import ONE_WAY_BANNER
    note = tmp_path / "Task Backlog.md"
    content = note.read_text()
    assert ONE_WAY_BANNER.strip() in content
    assert content.index(ONE_WAY_BANNER.strip()) < content.index("# Backlog — by category")
    assert "generated from the tasks ledger" in ONE_WAY_BANNER.lower()


@pytest.mark.anyio
async def test_render_is_atomic_and_a_short_write_leaves_the_old_file(db_session, tmp_path, monkeypatch):
    from pathlib import Path

    from app.integrations.tasks import render as render_mod
    from tests.legacy_backlog_importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    render_mod.write_backlog_note(db_session, user_id=1)
    before = note.read_bytes()

    real = Path.write_text

    def truncating(self, data, *a, **k):  # a full disk: the write "succeeds" short
        return real(self, data[: len(data) // 2], *a, **k)

    monkeypatch.setattr(Path, "write_text", truncating)
    with pytest.raises(RuntimeError, match="short write"):
        render_mod.write_backlog_note(db_session, user_id=1)
    assert note.read_bytes() == before, "the previous render survives a failed one"
    assert not note.with_name(note.name + ".tmp").exists()


# ---------------------------------------------------------------------------
# Someday / Waiting sections (E6a fold-in) — pulled out of the per-category
# render the same way routine rounds already are, so a domain section reads
# as actionable work only.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_someday_and_waiting_tasks_get_their_own_sections(db_session):
    from tests.legacy_backlog_importer import import_backlog, import_delegated, import_someday
    from app.integrations.tasks.render import render_backlog
    from datetime import date

    import_backlog(db_session, SAMPLE)
    import_someday(db_session, "# Someday\n\n## Ideas\n\n- [ ] Make a fountain 🔽\n")
    import_delegated(
        db_session,
        "# Delegated Tasks\n\n## [[Sam Rivers]]\n\n- [ ] **Chase the renewal** 🔺\n",
    )

    out = render_backlog(db_session, today=date(2026, 8, 30))

    assert "# Someday" in out
    assert "Make a fountain" in out
    assert "# Waiting" in out
    assert "## [[Sam Rivers]]" in out
    assert "Chase the renewal" in out

    # Pulled out, not duplicated: neither shows up under the ordinary
    # per-category sections (Someday/Waiting come after "Backlog — by
    # category" and the fountain/renewal titles must appear exactly once).
    assert out.count("Make a fountain") == 1
    assert out.count("Chase the renewal") == 1
    assert out.index("# Someday") > out.index("Backlog — by category")
    assert out.index("# Waiting") > out.index("# Someday")


@pytest.mark.anyio
async def test_waiting_section_groups_by_person(db_session):
    from tests.legacy_backlog_importer import import_delegated
    from app.integrations.tasks.render import render_backlog
    from datetime import date

    import_delegated(
        db_session,
        "# Delegated Tasks\n\n"
        "## [[Sam Rivers]]\n\n- [ ] **Chase the renewal** 🔺\n\n"
        "## [[Stef Murray]]\n\n- [ ] **Sound out Lyndsey** ⏫\n",
    )

    out = render_backlog(db_session, today=date(2026, 8, 30))

    assert out.index("## [[Sam Rivers]]") < out.index("Chase the renewal")
    assert out.index("Chase the renewal") < out.index("## [[Stef Murray]]")
    assert out.index("## [[Stef Murray]]") < out.index("Sound out Lyndsey")


@pytest.mark.anyio
async def test_someday_and_waiting_sections_are_absent_when_empty(db_session):
    """No parked ideas, nothing owed — no empty heading clutters the file."""
    from tests.legacy_backlog_importer import import_backlog
    from app.integrations.tasks.render import render_backlog
    from datetime import date

    import_backlog(db_session, SAMPLE)
    out = render_backlog(db_session, today=date(2026, 8, 30))

    assert "# Someday" not in out
    assert "# Waiting" not in out


# ---------------------------------------------------------------------------
# lios#224: unconfirmed (LLM-suggested) tasks get a `## Suggested` section
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_unconfirmed_tasks_render_in_their_own_suggested_section(db_session):
    from datetime import date, datetime, timezone

    from tests.legacy_backlog_importer import import_backlog
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.render import render_backlog

    import_backlog(db_session, SAMPLE)
    db_session.add(Task(
        uid="TASK-9001", title="A suggested task", status="next",
        source="seed", confirmed_at=None,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    out = render_backlog(db_session, today=date(2026, 8, 30))

    assert "# Suggested" in out
    assert "A suggested task" in out
    assert "tasks_confirm" in out
    # Not duplicated into the ordinary per-category sections.
    assert out.count("A suggested task") == 1
    assert out.index("# Suggested") > out.index("Backlog — by category")


@pytest.mark.anyio
async def test_suggested_section_is_absent_when_nothing_is_unconfirmed(db_session):
    from datetime import date

    from tests.legacy_backlog_importer import import_backlog
    from app.integrations.tasks.render import render_backlog

    import_backlog(db_session, SAMPLE)
    out = render_backlog(db_session, today=date(2026, 8, 30))

    assert "# Suggested" not in out


# ------------------------------------------------ one file per user, one window each

def _per_user(tmp_path, monkeypatch):
    """Each user's `Task Backlog.md` in its own vault directory, the way
    production has them — the shared-path `_at` would hide exactly the bug
    these tests are about."""
    def resolve(path, user_id_override=None):
        d = tmp_path / f"user{user_id_override}"
        d.mkdir(exist_ok=True)
        return d / "Task Backlog.md"
    monkeypatch.setattr("app.services.vault_paths.resolve", resolve)
    return resolve


def _own_a_task(session, title, *, as_user):
    from app.auth.context import use_user
    from app.integrations.tasks.tools import tasks_add_handler

    with use_user(as_user):
        tasks_add_handler(session, {"title": title})


@pytest.mark.anyio
async def test_each_users_render_is_independent_and_unconditional(
    db_session, tmp_path, monkeypatch,
):
    """Two users, two files, each rendered from THAT user's own view (PR
    #120). There is no shared drift state between them any more — the whole
    per-render digest/partition mechanism this used to exercise
    (`check_drift`, a 50-render window, actor-partitioned `RENDER_STATUS`
    rows) is gone (lios#231, 2026-09-14). What's left to prove is simpler
    and still true: rendering one user's file repeatedly must never affect,
    or be blocked by, the other's."""
    from app.integrations.tasks import render as render_mod
    from tests.legacy_backlog_importer import import_backlog

    resolve = _per_user(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    _own_a_task(db_session, "Alex's own line", as_user=1)

    render_mod.write_backlog_note(db_session, user_id=1)
    for _ in range(60):
        render_mod.write_backlog_note(db_session, user_id=2)

    assert resolve("", 1).read_text() != resolve("", 2).read_text()
    assert "Alex's own line" in resolve("", 1).read_text()
    assert "Alex's own line" not in resolve("", 2).read_text()

    render_mod.write_backlog_note(db_session, user_id=1)  # must not raise
    assert "Alex's own line" in resolve("", 1).read_text()


# ---------------------------------------------------------------------------
# kind + severity — the ledger is also the register of bugs and features
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_bug_renders_its_kind_and_severity_and_a_task_renders_nothing_extra(db_session):
    """`[bug/high]` after the title; a plain task's line is byte-identical to
    what it was before the register existed, so no existing line moves."""
    from datetime import date

    from tests.legacy_backlog_importer import import_backlog
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.render import render_backlog

    import_backlog(db_session, SAMPLE)
    before = render_backlog(db_session, today=date(2026, 8, 30), last_reviewed="2026-08-06")

    seed = db_session.query(Task).filter(Task.title.contains("Collect the seed")).one()
    seed.kind, seed.severity = "bug", "high"
    domain = db_session.query(Task).filter(Task.title.contains("Renew the domain")).one()
    domain.kind = "feature"
    db_session.commit()

    out = render_backlog(db_session, today=date(2026, 8, 30), last_reviewed="2026-08-06")
    assert "- [ ] **Collect the seed** [bug/high] ⏫ #home #errand 📅 2026-09-02" in out
    assert "- [ ] **Renew the domain** [feature] 🔽 #admin #person/isla" in out
    # Only those two lines changed.
    changed = [l for l in out.splitlines() if l not in before.splitlines()]
    assert len(changed) == 2
    assert "[task" not in out
