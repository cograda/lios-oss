"""Render the ledger back into `Task Backlog.md`.

The database is the source of truth and this file is a **strictly one-way,
write-only render** — the DB is never reconstructed from it. `render_backlog()`
existed before anything rewrote the real file so the round-trip could be
proven lossless by diffing an imported file against its own re-render; that
proof is done, and the file is now pure output.

⚠️ **2026-09-14 incident, and why this is unconditional.** The render used to
detect an external edit since the last render and refuse to regenerate unless
`force=True`. On 2026-09-14 one stray hand edit to the file caused six
`tasks_add` calls in a row to report `render_skipped: true` — the ledger and
the file drifted apart, which is exactly the failure the guard was meant to
prevent. Decided: the Loops app is the human-facing surface now; the markdown
exists for Obsidian search/backlinks only, and it must never block a write or
hold state. Every render overwrites the file unconditionally. Editing the file
by hand is harmless in the sense that nothing crashes — it is simply discarded
on the next render, which the file's own banner now says.

The preamble is a template rather than data. Its three ```tasks blocks are
Obsidian-plugin query lenses over the list below — views, not entries — and
the file's own header says so: *"Focus and This Week are query lenses (subsets
of the same list), not separate copies."* When a real tasks UI exists these
become SQL; until then they keep working exactly as they do today.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.integrations.tasks.models import Routine, Task, TaskEvent, TaskLink, TaskProject

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

{banner}
# Task Backlog

> **The single unified backlog.** The full list lives under **Backlog — by category** below — read that top-to-bottom for a complete review. **Focus** and **This Week** are query *lenses* (subsets of the same list), not separate copies. Ideas live in the **Someday** section below (`status="someday"`); items owed to us by others live in the **Waiting** section (`status="waiting"`) — `Someday.md` and `Delegated Tasks.md` are folded into the ledger and no longer written to. `⚠️TAG` = domain label to verify.

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

# ⚠️ The one-way banner. This file is generated from the tasks ledger on
# every write, unconditionally — a hand edit here is discarded on the very
# next render, whatever it was. Use Loops (the household's tasks app) or the
# `tasks_*` MCP tools to change anything. (2026-09-14: this used to be
# enforced by a drift guard that refused to re-render an externally-edited
# file; the guard itself caused a six-write outage and was removed — see the
# module docstring.)
ONE_WAY_BANNER = (
    "> ⚠️ **Generated from the tasks ledger — do not edit.** This file is "
    "rendered fresh from the household task backlog on every change. Any "
    "edit made directly in this file is silently discarded the next time it "
    "renders. Use the Loops app, or the `tasks_*` tools, to add or change "
    "anything.\n"
)


def _kind_marker(task: Task) -> str | None:
    """`[bug/high]`, `[feature]`, `[chore]` — or nothing for an ordinary task.

    `kind`/`severity` cover household bugs and features (lios development
    items are GitHub Issues on cograda/lios, not ledger rows), and the file
    must show which lines are which without a column of its own:
    one bracketed token after the title, kind then severity, so `[bug/high]`
    reads at a glance and sorts the same way every render. An ordinary task
    carries no marker, so every pre-existing line renders exactly as before.
    """
    if task.kind in (None, "task"):
        return None
    return f"[{task.kind}/{task.severity}]" if task.severity else f"[{task.kind}]"


def _task_line(task: Task, tags: list[str]) -> str:
    """One task, in the file's own notation."""
    box = "x" if task.status == "done" else " "
    # The title is stored with the file's own notation intact (bold,
    # [[wikilinks]]), so it goes back out untouched. Only the done-item
    # strikethrough is re-applied, because that is state, not text.
    title = f"~~{task.title}~~" if task.status == "done" else task.title

    parts = [f"- [{box}] {title}"]
    marker = _kind_marker(task)
    if marker:
        parts.append(marker)
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


def _owned_by(owner_id: int | None):
    """Filter for one person's view of the ledger: their own rows plus the
    UNOWNED ones (imported rows predate ownership; an unowned loop is
    nobody's and must stay visible to whoever looks). `None` is the whole
    household — the shape every test of the round-trip uses."""
    from sqlalchemy import or_, true

    if owner_id is None:
        return true()
    return or_(Task.owner_id == owner_id, Task.owner_id.is_(None))


def render_backlog(
    session: Session, *, today: date | None = None, last_reviewed: str | None = None,
    owner_id: int | None = None,
) -> str:
    """Render the backlog. Pure — returns text, writes nothing.

    `owner_id` renders one person's view (see `_owned_by`). The file lands in
    THAT person's vault, so it must not carry the other person's loops:
    until 2026-09-06 Sam's `Task Backlog.md` was a copy of the household
    ledger, 142 of its 201 lines Alex's.

    `last_reviewed` is passed in rather than defaulted to today: it is a
    property of a *sweep*, not of a render, so regenerating the file must not
    silently claim the backlog was reviewed because it was printed. It will
    come from the latest sweep record in `task_events`; until sweeps write
    those, the caller supplies the existing value.
    """
    today = today or date.today()

    # Rounds (routine_id set) get their own Routines section below rather
    # than salting every per-domain category with the same recurring line
    # every cycle — see `_routines_section`. Someday and waiting-on-someone
    # items get their own sections the same way (E6a fold-in) — otherwise a
    # domain's category section mixes actionable work with parked ideas and
    # things sitting with someone else, which is exactly the ambiguity a
    # dedicated section exists to remove.
    tasks = (
        session.query(Task)
        .filter(
            Task.routine_id.is_(None), Task.status.notin_(("someday", "waiting")),
            _owned_by(owner_id),
            # lios#224: an unconfirmed (LLM-suggested) task is not yet a
            # real backlog line — it renders in its own `## Suggested`
            # section instead (see `_suggested_section`).
            Task.confirmed_at.isnot(None),
        )
        .order_by(Task.sort_order)
        .all()
    )
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
        banner=ONE_WAY_BANNER,
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

    out.append(_someday_section(session, owner_id))
    out.append(_waiting_section(session, owner_id))
    out.append(_routines_section(session, owner_id))
    out.append(_suggested_section(session, owner_id))

    return "".join(out).rstrip("\n") + "\n"


def _someday_section(session: Session, owner_id: int | None = None) -> str:
    """Ideas and parked items — `status="someday"`. Folded in from
    `Someday.md` (E6a); pulled out of the per-domain sections the same way
    routine rounds are, so a category reads as actionable work only."""
    tasks = (
        session.query(Task)
        .filter(
            Task.routine_id.is_(None), Task.status == "someday", _owned_by(owner_id),
            Task.confirmed_at.isnot(None),
        )
        .order_by(Task.sort_order)
        .all()
    )
    if not tasks:
        return ""
    lines = ["\n---\n", "\n# Someday\n"]
    for task in tasks:
        lines.append("\n" + _task_line(task, _tags_for(task)))
    lines.append("\n")
    return "".join(lines)


UNSPECIFIED_PERSON = "Unspecified"


def _waiting_section(session: Session, owner_id: int | None = None) -> str:
    """Items owed to us by someone else — `status="waiting"`. Folded in from
    `Delegated Tasks.md` (E6a), grouped by the `waiting_on` person link the
    importer records (`task_links`, `target_type="person"`) — the same
    lossless name-not-FK shape the backlog importer already uses for domains,
    since there is no `people` table to resolve against."""
    tasks = (
        session.query(Task)
        .filter(
            Task.routine_id.is_(None), Task.status == "waiting", _owned_by(owner_id),
            Task.confirmed_at.isnot(None),
        )
        .order_by(Task.sort_order)
        .all()
    )
    if not tasks:
        return ""

    persons = {
        link.from_task_id: link.target_ref
        for link in session.query(TaskLink)
        .filter(TaskLink.target_type == "person", TaskLink.predicate == "waiting_on")
        .all()
    }

    by_person: dict[str, list[Task]] = {}
    for task in tasks:
        by_person.setdefault(persons.get(task.id) or UNSPECIFIED_PERSON, []).append(task)

    lines = ["\n---\n", "\n# Waiting\n"]
    for person in sorted(by_person):
        heading = f"[[{person}]]" if person != UNSPECIFIED_PERSON else person
        lines.append(f"\n## {heading}\n")
        for task in by_person[person]:
            lines.append("\n" + _task_line(task, _tags_for(task)))
    lines.append("\n")
    return "".join(lines)


def _routines_section(session: Session, owner_id: int | None = None) -> str:
    """Active routines: title, schedule, owner, next due, current round.
    Rounds themselves are excluded from the category sections above (they
    render here instead) so a recurring chore doesn't salt every domain with
    the same line every cycle."""
    from app.models.users import User

    rq = session.query(Routine).filter(Routine.active.is_(True))
    if owner_id is not None:
        from sqlalchemy import or_
        rq = rq.filter(or_(Routine.default_owner_id == owner_id, Routine.default_owner_id.is_(None)))
    routines = rq.order_by(Routine.id).all()
    if not routines:
        return ""

    users = {u.id: u.display_name for u in session.query(User).all()}
    open_statuses = ("inbox", "next", "waiting", "scheduled")
    lines = ["\n---\n", "\n# Routines\n"]
    for r in routines:
        round_task = (
            session.query(Task)
            .filter(Task.routine_id == r.id, Task.status.in_(open_statuses))
            .order_by(Task.id.desc())
            .first()
        )
        owner = users.get(r.default_owner_id, "unassigned") if r.default_owner_id else "unassigned"
        schedule = f"{r.schedule_kind}: {r.schedule_spec}"
        if round_task is None:
            next_due, current = "—", "—"
        else:
            next_due = round_task.defer_until.date().isoformat() if round_task.defer_until else "due now"
            current = f"{round_task.uid} ({round_task.status})"
        lines.append(f"\n- **{r.title}** — {schedule} · owner: {owner} · next due: {next_due} · current round: {current}\n")
    return "".join(lines)


def _suggested_section(session: Session, owner_id: int | None = None) -> str:
    """lios#224: unconfirmed (LLM-suggested, not yet reviewed) tasks — the
    code gate's visible surface in the rendered view. Not filed under a
    category or project section (an unconfirmed suggestion has no place in
    the backlog yet, by design); listed here with the one-line pointer to
    `tasks_confirm` so a person reading the file top-to-bottom knows these
    need a decision, not that they were silently dropped."""
    tasks = (
        session.query(Task)
        .filter(
            Task.routine_id.is_(None), _owned_by(owner_id),
            Task.confirmed_at.is_(None),
        )
        .order_by(Task.sort_order)
        .all()
    )
    if not tasks:
        return ""
    lines = [
        "\n---\n", "\n# Suggested\n",
        "\n> Not yet confirmed — call `tasks_confirm(uid=...)` to make one of "
        "these active, or `tasks_decline(uid=...)` to dismiss it.\n",
    ]
    for task in tasks:
        source = task.source or "unknown"
        created = task.created_at.date().isoformat() if task.created_at else "unknown"
        lines.append(f"\n- {task.uid} — {task.title} (source: {source}, created: {created})")
    lines.append("\n")
    return "".join(lines)


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


def write_backlog_note(session: Session, user_id: int | None = None) -> str:
    """Regenerate `Task Backlog.md` from the ledger. Returns the vault path.

    ⚠️ Unconditional. This overwrites the file every time, full stop — there
    is no longer an edit-detection step here (removed 2026-09-14; see the
    module docstring for the incident that caused its removal). Hand edits to
    the file are lost on the next write, and the way to change a task is to
    change the row. The file's own banner (`ONE_WAY_BANNER`) says so.

    Two guards remain, and neither is about human edits — both protect
    against the render itself being bad:
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

    existing = last_reviewed_date(session)
    # The file is one person's vault view, so it renders THAT person's rows.
    # `user_id` is the scheduler's explicit binding (`render_all_vaults`);
    # a request-scoped write has the caller bound instead.
    from app.auth.context import current_user_id

    owner_id = user_id if user_id is not None else current_user_id()
    content = render_backlog(session, last_reviewed=existing, owner_id=owner_id)
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

    return BACKLOG_NOTE_PATH
