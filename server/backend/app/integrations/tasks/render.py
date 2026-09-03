"""Render the ledger back into `Task Backlog.md`.

The database is the source of truth and this file becomes a one-way
projection — but **only once the round-trip is lossless**, which is why
`render_backlog()` exists before anything rewrites the real file. Import the
live file, render it back, diff the two: what the diff shows is exactly what
the schema cannot yet hold. Making the file generated before that diff is
empty would delete the difference from the only copy there is.

The preamble is a template rather than data. Its three ```tasks blocks are
Obsidian-plugin query lenses over the list below — views, not entries — and
the file's own header says so: *"Focus and This Week are query lenses (subsets
of the same list), not separate copies."* When a real tasks UI exists these
become SQL; until then they keep working exactly as they do today.
"""

from __future__ import annotations

import hashlib
from datetime import date

from sqlalchemy.orm import Session

from app.integrations.tasks.models import Task, TaskEvent, TaskLink, TaskProject

BACKLOG_NOTE_PATH = "Task Backlog.md"

# Emitted back out as the file's own priority emoji. `lowest` has no glyph
# because the file has never used one.
PRIORITY_EMOJI = {"highest": "🔺", "high": "⏫", "medium": "🔼", "low": "🔽"}

# The file's category order, which is neither alphabetical nor by size. It
# lives here rather than in the database because `domains` is empty in
# production, so there is no row to hang an ordering off yet — see the
# importer's note. When domains are populated this becomes a column.
CATEGORY_ORDER = ["Home", "Renovation", "Kids", "Finance", "Admin"]

# ⚠️ A task with no category MUST still render. `tasks_add` creates no domain
# link, so without this heading a newly added task is written to the ledger,
# reported as created, and is invisible in the file — the exact silent-loss
# shape this project keeps finding. It renders under its own heading instead,
# which also makes uncategorised tasks obvious enough to get filed.
UNCATEGORISED = "Uncategorised"

PREAMBLE = """---
title: Task Backlog
type: note
created: 2026-03-24
modified: {modified}
last-reviewed: {last_reviewed}
tags: []
---

# Task Backlog

> **The single unified backlog.** The full list lives under **Backlog — by category** below — read that top-to-bottom for a complete review. **Focus** and **This Week** are query *lenses* (subsets of the same list), not separate copies. Ideas live in [[Someday]]; items owed to us by others in [[Delegated Tasks]]. `⚠️TAG` = domain label to verify.

---

## 🎯 Today's Focus

> Set by `/lock-in` (3-5 items, tagged `#focus`). Mirrors today's daily note.

```tasks
not done
path includes Task Backlog.md
tag includes #focus
```

---

## 📌 This Week — active

> Set deliberately (tagged `#week`), not inferred from priority — in a real backlog the top two priorities cover most of the list, so "this week" would silently mean "everything". Focus is a subset of the week. A lens — take an item out of the week and it drops out of this view automatically.

```tasks
not done
path includes Task Backlog.md
tag includes #week
sort by priority
```

---

## 📺 Sit-down list

> Things that get done **sitting in front of the TV with a laptop** — no phone calls, no
> standing up, no tools. A **context**, not a priority: an evening where nothing physical
> is going to happen is still an evening where these move.

```tasks
not done
path includes Task Backlog.md
tag includes #sitdown
sort by priority
```

---

# Backlog — by category
"""


def _task_line(task: Task, tags: list[str]) -> str:
    """One task, in the file's own notation."""
    box = "x" if task.status == "done" else " "
    # The title is stored with the file's own notation intact (bold,
    # [[wikilinks]]), so it goes back out untouched. Only the done-item
    # strikethrough is re-applied, because that is state, not text.
    title = f"~~{task.title}~~" if task.status == "done" else task.title

    parts = [f"- [{box}] {title}"]
    if task.priority and task.priority in PRIORITY_EMOJI:
        parts.append(PRIORITY_EMOJI[task.priority])
    parts.extend(tags)
    if task.due_at:
        parts.append(f"📅 {task.due_at.date().isoformat()}")
    if task.status == "done" and task.completed_at:
        parts.append(f"✅ {task.completed_at.date().isoformat()}")

    line = " ".join(parts)
    if task.description:
        for para in task.description.split("\n"):
            if para.strip():
                body = para.strip()
                line += "\n  " + (body if body.startswith("- ") else f"- {body}")
    return line


# The two queue tags. Emitted from the `queue` column, never stored in `tags`,
# so the file's Focus and This Week lenses read the same value the app does.
QUEUE_TAGS = {"week": ["#week"], "focus": ["#week", "#focus"]}


def _tags_for(task: Task) -> list[str]:
    """The task's tags, in file order, plus the queue tags its `queue` implies.

    Stored verbatim rather than reconstructed from the columns they populate:
    #errand and #quick also become `context` and `energy`, but the file
    carries many more (#home-automation, #person/isla, #movein) that have no
    column, and rebuilding the line from columns alone dropped every one.

    Queue tags are the one exception, appended from the column and deduplicated
    against anything the stored tags already carry, because a `#focus` typed in
    the file before queues existed must not render twice.
    """
    tags = list(task.tags or [])
    for tag in QUEUE_TAGS.get(task.queue or "", []):
        if tag not in tags:
            tags.append(tag)
    return tags


def _heading_for(project: TaskProject) -> str:
    """The section heading, with its wikilink put back where it was.

    ⚠️ A heading is not always just a link: `## [[Comar]] — Home Server & Dev`
    has a suffix after it, and emitting only the link silently dropped four
    words. The stored title is the heading with link syntax removed, so the
    syntax goes back on the part it came from.
    """
    if not project.note_path:
        return project.title
    if project.title.startswith(project.note_path):
        return f"[[{project.note_path}]]" + project.title[len(project.note_path):]
    return f"[[{project.note_path}]]"


def render_backlog(
    session: Session, *, today: date | None = None, last_reviewed: str | None = None,
) -> str:
    """Render the whole backlog. Pure — returns text, writes nothing.

    `last_reviewed` is passed in rather than defaulted to today: it is a
    property of a *sweep*, not of a render, so regenerating the file must not
    silently claim the backlog was reviewed because it was printed. It will
    come from the latest sweep record in `task_events`; until sweeps write
    those, the caller supplies the existing value.
    """
    today = today or date.today()

    tasks = session.query(Task).order_by(Task.sort_order).all()
    projects = {p.id: p for p in session.query(TaskProject).all()}

    # The H1 category lives on a link rather than a column because `domains`
    # is empty; one query rather than a join per task.
    category_of: dict[int, str] = {
        link.from_task_id: link.target_ref
        for link in session.query(TaskLink).filter(TaskLink.target_type == "domain").all()
    }

    out = [PREAMBLE.format(
        modified=today.isoformat(),
        # Never falls back to today: an unswept backlog says so by omission
        # rather than by asserting it was reviewed the day it was printed.
        last_reviewed=last_reviewed or "",
    )]

    by_category: dict[str, list[Task]] = {}
    for task in tasks:
        by_category.setdefault(category_of.get(task.id) or UNCATEGORISED, []).append(task)

    ordered = [c for c in CATEGORY_ORDER if c in by_category]
    ordered += sorted(
        c for c in by_category if c and c not in CATEGORY_ORDER and c != UNCATEGORISED
    )
    if UNCATEGORISED in by_category:
        ordered.append(UNCATEGORISED)

    for cat_index, category in enumerate(ordered):
        if cat_index:
            out.append("\n---\n")
        out.append(f"\n# {category}\n")

        current_project: int | None = -1
        current_subsection: str | None = None
        for task in by_category[category]:
            if task.project_id != current_project:
                current_project = task.project_id
                current_subsection = None
                project = projects.get(task.project_id) if task.project_id else None
                if project:
                    out.append(f"\n## {_heading_for(project)}\n")
                    if project.body_note:
                        out.append(f"\n{project.body_note}\n")

            if task.subsection != current_subsection:
                current_subsection = task.subsection
                if current_subsection:
                    out.append(f"\n### {current_subsection}\n")

            out.append("\n" + _task_line(task, _tags_for(task)))

        out.append("\n")

    return "".join(out).rstrip("\n") + "\n"


SWEEP_STATUS = "swept"


def last_reviewed_date(session: Session) -> str | None:
    """The date of the newest sweep, or None if the backlog has never been swept.

    Computed, never stamped. A render is not a review, so regenerating the file
    must not move this date — only `/backlog-sweep` writing a sweep event does.
    """
    event = (
        session.query(TaskEvent)
        .filter(TaskEvent.to_status == SWEEP_STATUS, TaskEvent.task_id.is_(None))
        .order_by(TaskEvent.at.desc())
        .first()
    )
    return event.at.date().isoformat() if event else None


def record_sweep(session: Session, *, actor_id: int | None = None, note: str | None = None) -> TaskEvent:
    """Record that the backlog was reviewed. This — not a render — is what
    moves `last-reviewed`."""
    event = TaskEvent(
        task_id=None, from_status=None, to_status=SWEEP_STATUS,
        actor_id=actor_id, note=note,
    )
    session.add(event)
    session.flush()
    return event


RENDER_STATUS = "rendered"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def last_rendered_digest(session: Session) -> str | None:
    """The sha256 of the file as we last left it, or None if never rendered."""
    event = (
        session.query(TaskEvent)
        .filter(TaskEvent.task_id.is_(None), TaskEvent.to_status == RENDER_STATUS)
        .order_by(TaskEvent.at.desc(), TaskEvent.id.desc())
        .first()
    )
    return event.note if event else None


def recent_rendered_digests(session: Session, limit: int = 50) -> list[str]:
    """The sha256s of the last `limit` renders, newest first.

    ⚠️ The drift check compares against ALL of these, not just the latest, and
    the reason is a race observed in production on 2026-09-02: two writes
    four seconds apart (completing tasks in quick succession) rendered R1 and
    then R2; Syncthing, mid-way through propagating R1 to the laptop, echoed
    R1 back over R2. The file on disk was then byte-identical to a render
    comar itself had made — and the guard, comparing only against R2, called
    it a hand edit and refused every subsequent write. Nothing a person typed
    was at risk; the lock-out was the whole cost. A file that matches any
    recent render carries no hand edit, so it is clean, and the next write
    simply re-renders it.
    """
    rows = (
        session.query(TaskEvent.note)
        .filter(TaskEvent.task_id.is_(None), TaskEvent.to_status == RENDER_STATUS)
        .order_by(TaskEvent.at.desc(), TaskEvent.id.desc())
        .limit(limit)
        .all()
    )
    return [r[0] for r in rows if r[0]]


def check_drift(session: Session, user_id: int | None = None) -> dict:
    """Has the file changed since we wrote it?

    A one-way generated file has a failure mode a two-way one does not: you
    open it in Obsidian out of habit, tick a box, and the next write silently
    destroys the edit. Nothing errors, because overwriting is exactly what
    the renderer is for. The tick is simply gone, and it looks like the task
    was never done.

    So the render compares before writing. `state` is one of:

      - `clean`      the file is byte-identical to what we wrote
      - `stale`      the file matches an EARLIER render of ours — a sync echo
                     overwrote a newer one; nothing hand-typed is at risk and
                     the next write repairs it (see `recent_rendered_digests`)
      - `drifted`    someone edited it — their change is about to be lost
      - `unknown`    we have never rendered, so there is nothing to compare
      - `missing`    no file on disk; writing one is not destroying anything
    """
    from app.services.vault_paths import resolve

    note_abs = resolve(BACKLOG_NOTE_PATH, user_id_override=user_id)
    if not note_abs.exists():
        return {"state": "missing", "path": BACKLOG_NOTE_PATH}
    # An EMPTY file is never a hand edit — nobody opens a 130 KB backlog and
    # saves nothing. It is what a write leaves behind when the disk is full
    # (2026-09-02 17:29:11, the moment the host hit 100 %: a 0-byte render,
    # then "drifted", then every write refused to protect an edit that did
    # not exist). Treat it as missing: writing over nothing destroys nothing.
    if note_abs.stat().st_size == 0:
        return {"state": "missing", "path": BACKLOG_NOTE_PATH}
    known = recent_rendered_digests(session)
    if not known:
        return {"state": "unknown", "path": BACKLOG_NOTE_PATH}
    actual = _digest(note_abs.read_text(encoding="utf-8"))
    # Matching an OLDER render is a sync echo, not a hand edit — see
    # `recent_rendered_digests`. Reported distinctly so a caller can tell.
    if actual == known[0]:
        state = "clean"
    elif actual in known:
        state = "stale"
    else:
        state = "drifted"
    return {"state": state, "path": BACKLOG_NOTE_PATH}


def write_backlog_note(
    session: Session,
    user_id: int | None = None,
    *,
    force: bool = False,
) -> str:
    """Regenerate `Task Backlog.md` from the ledger. Returns the vault path.

    ⚠️ This overwrites the file. From the first call the file is OUTPUT: hand
    edits to it are lost on the next write, and the way to change a task is to
    change the row. That is the whole point — but it is also why nothing called
    this until the round-trip was proved lossless against the live file.
    """
    from app.services.vault_paths import resolve

    # ⚠️ Refuse to write an empty backlog over a non-empty file. The failure
    # this prevents is specific and unrecoverable: the ledger is reachable but
    # unpopulated (migrations applied, import never run; a restored database;
    # a mistyped filter), the render is therefore valid-but-empty, and it
    # overwrites the only copy of 246 tasks with a preamble. The file is
    # gitignored and Drive-synced, so there is no history to recover from.
    # An empty ledger is allowed to write only over an absent or empty file.
    task_count = session.query(Task).count()
    note_abs = resolve(BACKLOG_NOTE_PATH, user_id_override=user_id)
    if task_count == 0 and note_abs.exists() and note_abs.stat().st_size > 0:
        raise RuntimeError(
            f"refusing to render an empty ledger over {BACKLOG_NOTE_PATH} "
            f"({note_abs.stat().st_size} bytes on disk). Import first."
        )

    # ⚠️ Refuse to overwrite a hand edit. See `check_drift` — the edit is not
    # recoverable once written over, and the file is gitignored and
    # Drive-synced, so nothing else holds a copy either. `force=True` is the
    # deliberate "yes, discard it" and is never the default.
    if not force and check_drift(session, user_id)["state"] == "drifted":
        raise RuntimeError(
            f"{BACKLOG_NOTE_PATH} has been edited since it was last rendered. "
            f"The database is the source of truth, so rendering would discard "
            f"that edit. Reconcile it into the ledger first, or pass "
            f"force=True to discard it deliberately."
        )

    existing = last_reviewed_date(session)
    content = render_backlog(session, last_reviewed=existing)
    # Atomic: write beside, then rename. A plain write_text on a full disk
    # leaves a truncated file in place of the real one — and Syncthing then
    # carries the truncation to every device. rename() either succeeds whole
    # or leaves the previous file untouched.
    tmp = note_abs.with_name(note_abs.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    if tmp.stat().st_size != len(content.encode("utf-8")):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"short write rendering {BACKLOG_NOTE_PATH} — disk full?")
    tmp.replace(note_abs)

    # Recorded AFTER a successful write. Stamping it first would mark the file
    # clean when the write had failed, so the next render would sail past the
    # guard it exists to trip.
    session.add(TaskEvent(
        task_id=None, from_status=None, to_status=RENDER_STATUS,
        note=_digest(content),
    ))
    session.flush()
    return BACKLOG_NOTE_PATH
