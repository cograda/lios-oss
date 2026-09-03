"""One-way import of `Task Backlog.md` into the ledger.

Idempotent on `Task.import_key`: re-running updates the rows it already
created rather than duplicating them, so a dry run, a fix and a real run are
all safe in any order.

🔑 **What this deliberately does not do.**

- It never writes `created_at`. For every imported item the creation date is
  unrecoverable — the vault is gitignored and Drive-synced, so there is no
  history to mine, and only 28% of items carry any date at all. Stamping the
  import date would manufacture a fact and quietly corrupt every aging and
  throughput number computed afterwards. NULL means unknown and says so.
- It never invents a domain. `domains` is **empty in production**, and its
  rows require an operational definition and a scope note — content decisions,
  not import decisions. The H1 category is preserved losslessly as a
  `task_link` (`target_type='domain'`, the name as `target_ref`); resolving
  those to foreign keys once real domains exist is a query, not a re-import.
- It never guesses. Every record here is something the file states outright,
  so every link is `derived_by='deterministic'` at confidence 1.0. Inferred
  edges (`blocked_by`, `waiting_on`) remain the LLM tier's business, opt-in
  and separate, in `scripts/taskgraph_parse.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone

from sqlalchemy.orm import Session

from app.integrations.tasks.models import Task, TaskEvent, TaskLink, TaskProject
from app.integrations.tasks.parse import ParsedTask, parse_document


TITLE_MAX = 300


def split_title(title: str) -> tuple[str, str | None]:
    """Split an over-long task line into a title and an overflow remainder.

    ⚠️ 12 of the 246 lines in the live file exceed 300 characters, because the
    file's task lines are not titles — they are a title followed by prose
    ("… — her 28 Jul mail confirms a final valuation from then. Terms: …").
    Truncating them silently drops real commitments: one loses *"AIB now needs
    it"*, another *"Blocked on tooling"*.

    So the overflow is moved into the description instead of being discarded.
    The cut prefers a sentence end, then an em-dash clause, then a word
    boundary — never mid-word — so the title stays a readable label and
    `title + overflow` still contains every character of the original.
    """
    if len(title) <= TITLE_MAX:
        return title, None

    window = title[:TITLE_MAX]
    for boundary in (". ", " — ", " - "):
        idx = window.rfind(boundary)
        if idx > TITLE_MAX // 2:
            cut = idx + (1 if boundary == ". " else 0)
            return title[:cut].strip(), title[cut:].strip()

    idx = window.rfind(" ")
    cut = idx if idx > TITLE_MAX // 2 else TITLE_MAX
    return title[:cut].strip(), title[cut:].strip()


@dataclass
class ImportResult:
    parsed: int = 0
    tasks_created: int = 0
    tasks_updated: int = 0
    projects_created: int = 0
    links_created: int = 0
    events_created: int = 0

    def as_dict(self) -> dict:
        return {
            "parsed": self.parsed,
            "tasks_created": self.tasks_created,
            "tasks_updated": self.tasks_updated,
            "projects_created": self.projects_created,
            "links_created": self.links_created,
            "events_created": self.events_created,
        }


def _next_uid(session: Session, model, prefix: str) -> int:
    """Highest existing sequence number for `prefix`, or 0.

    uids are allocated in one contiguous run per import rather than per row so
    the numbering follows file order, which is the order a person reads.
    """
    rows = session.query(model.uid).filter(model.uid.like(f"{prefix}-%")).all()
    nums = [int(r[0].split("-")[1]) for r in rows if r[0].split("-")[1].isdigit()]
    return max(nums, default=0)


def _as_utc(date_str: str | None) -> datetime | None:
    """A bare `2026-08-30` becomes midday UTC, not midnight.

    Midnight is ambiguous across a timezone boundary — an Irish-summer date
    stamped 00:00 UTC reads as the previous day locally, which would silently
    shift every due date in the file by one day for half the year.
    """
    if not date_str:
        return None
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime.combine(d, time(12, 0), tzinfo=timezone.utc)


def _ensure_project(
    session: Session, parsed: ParsedTask, cache: dict[str, TaskProject],
    result: ImportResult,
) -> TaskProject | None:
    """Get or create the H2 section as a project.

    The H2 is the file's *only* expression of hierarchy — there are no indented
    sub-tasks anywhere in it — so it is the whole of the structure being
    imported, not a nicety.
    """
    if not parsed.section:
        return None
    if parsed.section in cache:
        return cache[parsed.section]

    project = (
        session.query(TaskProject)
        .filter(TaskProject.title == parsed.section)
        .one_or_none()
    )
    if project is None:
        seq = _next_uid(session, TaskProject, "PROJ") + 1
        project = TaskProject(
            uid=f"PROJ-{seq:04d}",
            title=parsed.section,
            status="active",
            # The [[wikilink]] a person typed, kept verbatim: it is the join
            # back to the vault note and is confidence 1.0 by construction.
            note_path=parsed.section_note,
        )
        session.add(project)
        session.flush()
        result.projects_created += 1

    cache[parsed.section] = project
    return project


def import_backlog(
    session: Session, content: str, *, dry_run: bool = False,
) -> ImportResult:
    """Import backlog markdown into the ledger. Idempotent on import_key."""
    result = ImportResult()
    parsed_tasks, parsed_sections = parse_document(content)
    result.parsed = len(parsed_tasks)

    project_cache: dict[str, TaskProject] = {}
    # Section prose and ordering first, so a task's project already carries
    # them by the time the task references it.
    section_meta = {s.title: s for s in parsed_sections}
    next_seq = _next_uid(session, Task, "TASK")

    for parsed in parsed_tasks:
        project = _ensure_project(session, parsed, project_cache, result)
        if project is not None and parsed.section in section_meta:
            meta = section_meta[parsed.section]
            project.body_note = meta.body_note
            project.sort_order = meta.order

        task = (
            session.query(Task)
            .filter(Task.import_key == parsed.import_key)
            .one_or_none()
        )
        is_new = task is None
        if is_new:
            next_seq += 1
            task = Task(uid=f"TASK-{next_seq:04d}", import_key=parsed.import_key)
            session.add(task)
            result.tasks_created += 1
        else:
            result.tasks_updated += 1

        title, overflow = split_title(parsed.title)
        task.title = title
        # Overflow leads the description: it is the tail of the sentence the
        # title started, so it reads before the sub-bullets, not after them.
        task.description = "\n\n".join(
            part for part in (overflow, parsed.description) if part
        ) or None
        # Everything in this file is an actionable backlog item. Splitting
        # `next` from `someday` is a triage judgement, not something the file
        # states — Someday.md is the separate list that carries that meaning.
        task.status = "done" if parsed.completed else "next"
        task.priority = parsed.priority
        task.project_id = project.id if project else None
        task.tags = parsed.tags
        task.subsection = parsed.subsection
        task.sort_order = parsed.order
        task.context = parsed.context
        task.energy = parsed.energy
        task.due_at = _as_utc(parsed.due_date)
        task.completed_at = _as_utc(parsed.done_date)
        task.source = "backlog_import"
        # created_at is left untouched — see the module docstring.
        session.flush()

        if is_new:
            # The only historical fact the file preserves: a ✅ date. Where one
            # exists the event is stamped with it; where it does not, a done
            # item gets no event rather than one dated today, for the same
            # reason created_at stays NULL.
            if parsed.completed and parsed.done_date:
                session.add(TaskEvent(
                    task_id=task.id, from_status=None, to_status="done",
                    at=_as_utc(parsed.done_date),
                    note="imported from Task Backlog.md",
                ))
                result.events_created += 1

            links = _links_for(parsed)
            for target_type, target_ref, predicate in links:
                session.add(TaskLink(
                    from_task_id=task.id, target_type=target_type,
                    target_ref=target_ref, predicate=predicate,
                    confidence=1.0, derived_by="deterministic",
                ))
            result.links_created += len(links)

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return result


def _links_for(parsed: ParsedTask) -> list[tuple[str, str, str]]:
    """Links the file states outright. Nothing inferred."""
    links: list[tuple[str, str, str]] = []

    # The H1 category. `domains` is empty, so this is carried as a name rather
    # than an FK — losslessly, and resolvable later by query.
    if parsed.category:
        links.append(("domain", parsed.category, "concerns"))

    # [[wikilinks]] in the task text. A person typed them, so they are the
    # highest-confidence signal in the file and no model second-guesses them.
    # The far side may be a person, a project note or a note that does not
    # exist yet — target_type stays `note` because resolving which it is
    # requires reading the vault, and a wrong guess here is permanent.
    for link in parsed.wikilinks:
        links.append(("note", link, "concerns"))

    return links
