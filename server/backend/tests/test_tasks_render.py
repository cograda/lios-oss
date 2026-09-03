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

    from app.integrations.tasks.importer import import_backlog
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

    from app.integrations.tasks.importer import import_backlog
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
    from app.integrations.tasks.importer import import_backlog

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

    from app.integrations.tasks.importer import import_backlog
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.render import render_backlog

    import_backlog(db_session, SAMPLE)
    db_session.add(Task(uid="TASK-9001", title="**Orphan with no category**", status="next"))
    db_session.commit()

    out = render_backlog(db_session)

    assert len(re.findall(r"^- \[", out, re.M)) == db_session.query(Task).count()
    assert "**Orphan with no category**" in out
    assert "# Uncategorised" in out


# --------------------------------------------------------------- drift guard

def _at(tmp_path, monkeypatch):
    note = tmp_path / "Task Backlog.md"
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    return note


@pytest.mark.anyio
async def test_a_hand_edit_is_refused_rather_than_overwritten(
    db_session, tmp_path, monkeypatch,
):
    """The failure this exists to stop: you tick a box in Obsidian out of
    habit and the next write destroys it, silently, because overwriting is
    precisely the renderer's job. Nothing errors and the tick is simply gone,
    which reads as the task never having been done."""
    from app.integrations.tasks import render as render_mod
    from app.integrations.tasks.importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    render_mod.write_backlog_note(db_session, user_id=1)
    assert render_mod.check_drift(db_session, user_id=1)["state"] == "clean"

    note.write_text(note.read_text() + "\n- [x] typed straight into the file\n")
    assert render_mod.check_drift(db_session, user_id=1)["state"] == "drifted"

    with pytest.raises(RuntimeError, match="edited since it was last rendered"):
        render_mod.write_backlog_note(db_session, user_id=1)
    assert "typed straight into the file" in note.read_text()


@pytest.mark.anyio
async def test_force_discards_the_edit_deliberately(db_session, tmp_path, monkeypatch):
    from app.integrations.tasks import render as render_mod
    from app.integrations.tasks.importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    render_mod.write_backlog_note(db_session, user_id=1)
    note.write_text(note.read_text() + "\nhand edit\n")

    render_mod.write_backlog_note(db_session, user_id=1, force=True)
    assert "hand edit" not in note.read_text()
    assert render_mod.check_drift(db_session, user_id=1)["state"] == "clean"


@pytest.mark.anyio
async def test_never_rendered_reads_as_unknown_not_drifted(
    db_session, tmp_path, monkeypatch,
):
    """An absent baseline must not read as a hand edit. Otherwise the first
    render after a database restore refuses, citing an edit nobody made."""
    from app.integrations.tasks import render as render_mod
    from app.integrations.tasks.importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    note.write_text("pre-existing, and not ours\n")
    import_backlog(db_session, SAMPLE)

    assert render_mod.check_drift(db_session, user_id=1)["state"] == "unknown"
    render_mod.write_backlog_note(db_session, user_id=1)


@pytest.mark.anyio
async def test_a_failed_write_does_not_leave_the_file_marked_clean(
    db_session, tmp_path, monkeypatch,
):
    """The digest is stamped after the write, not before. Stamping first
    marks a file clean that was never written, so the next render sails past
    the guard that exists to catch exactly that."""
    from app.integrations.tasks import render as render_mod

    _at(tmp_path, monkeypatch)
    # Empty ledger over an absent file: allowed to write, nothing destroyed.
    assert render_mod.last_rendered_digest(db_session) is None
    render_mod.write_backlog_note(db_session, user_id=1)
    assert render_mod.last_rendered_digest(db_session) is not None


@pytest.mark.anyio
async def test_a_sync_echo_of_an_earlier_render_is_not_a_hand_edit(
    db_session, tmp_path, monkeypatch,
):
    """Observed 2026-09-02: two writes four seconds apart, and Syncthing echoed
    the first render back over the second. The disk then matched a render
    comar itself had made, and the guard — comparing only against the latest
    — called it a hand edit and refused every write. A file matching ANY
    recent render carries nothing a person typed, so it must not lock the
    ledger; it reads as `stale` and the next write repairs it."""
    from app.integrations.tasks import render as render_mod
    from app.integrations.tasks.importer import import_backlog
    from app.integrations.tasks.models import Task

    note = _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    render_mod.write_backlog_note(db_session, user_id=1)
    first = note.read_text()

    task = db_session.query(Task).first()
    task.title = task.title + " (renamed)"
    db_session.commit()
    render_mod.write_backlog_note(db_session, user_id=1)
    assert note.read_text() != first

    note.write_text(first)  # the echo
    assert render_mod.check_drift(db_session, user_id=1)["state"] == "stale"
    render_mod.write_backlog_note(db_session, user_id=1)  # must not raise
    assert "(renamed)" in note.read_text()
    assert render_mod.check_drift(db_session, user_id=1)["state"] == "clean"


@pytest.mark.anyio
async def test_an_empty_file_is_missing_not_drifted(db_session, tmp_path, monkeypatch):
    """2026-09-02 17:29:11: the host disk hit 100 % mid-render, the file was
    left at 0 bytes, the guard called that a hand edit, and every write was
    refused to protect nothing. An empty generated file is never an edit."""
    from app.integrations.tasks import render as render_mod
    from app.integrations.tasks.importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    render_mod.write_backlog_note(db_session, user_id=1)
    note.write_text("")
    assert render_mod.check_drift(db_session, user_id=1)["state"] == "missing"
    render_mod.write_backlog_note(db_session, user_id=1)  # must not raise
    assert note.stat().st_size > 0


@pytest.mark.anyio
async def test_render_is_atomic_and_a_short_write_leaves_the_old_file(db_session, tmp_path, monkeypatch):
    from pathlib import Path

    from app.integrations.tasks import render as render_mod
    from app.integrations.tasks.importer import import_backlog

    note = _at(tmp_path, monkeypatch)
    import_backlog(db_session, SAMPLE)
    render_mod.write_backlog_note(db_session, user_id=1)
    before = note.read_bytes()

    real = Path.write_text

    def truncating(self, data, *a, **k):  # a full disk: the write "succeeds" short
        return real(self, data[: len(data) // 2], *a, **k)

    monkeypatch.setattr(Path, "write_text", truncating)
    with pytest.raises(RuntimeError, match="short write"):
        render_mod.write_backlog_note(db_session, user_id=1, force=True)
    assert note.read_bytes() == before, "the previous render survives a failed one"
    assert not note.with_name(note.name + ".tmp").exists()
