"""Test-only relic of the one-way `Task Backlog.md` -> ledger importer.

See `legacy_backlog_parse.py`'s module docstring for why this exists: moved
out of `app/integrations/tasks/importer.py` on 2026-09-15 (the ledger became
the tasks system's sole source of truth; nothing in production reads
`Task Backlog.md` any more), kept here verbatim ONLY because a large slice
of the existing test suite uses `import_backlog`/`import_someday`/
`import_delegated` as a convenient way to seed realistic ledger rows from a
markdown fixture string. Not imported by any `app/` code, not reachable via
any tool or route. Do not add new callers outside `tests/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone

from sqlalchemy.orm import Session

from app.integrations.tasks.models import Task, TaskEvent, TaskLink, TaskProject
from tests.legacy_backlog_parse import ParsedTask, parse_document, parse_grouped_list


TITLE_MAX = 300


def split_title(title: str) -> tuple[str, str | None]:
    """Split an over-long task line into a title and an overflow remainder."""
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
    """Highest existing sequence number for `prefix`, or 0."""
    rows = session.query(model.uid).filter(model.uid.like(f"{prefix}-%")).all()
    nums = [int(r[0].split("-")[1]) for r in rows if r[0].split("-")[1].isdigit()]
    return max(nums, default=0)


def _as_utc(date_str: str | None) -> datetime | None:
    """A bare `2026-08-30` becomes midday UTC, not midnight."""
    if not date_str:
        return None
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime.combine(d, time(12, 0), tzinfo=timezone.utc)


def _ensure_project(
    session: Session, parsed: ParsedTask, cache: dict[str, TaskProject],
    result: ImportResult,
) -> TaskProject | None:
    """Get or create the H2 section as a project."""
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
        task.description = "\n\n".join(
            part for part in (overflow, parsed.description) if part
        ) or None
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
        if is_new:
            task.confirmed_at = datetime.now(timezone.utc)
        session.flush()

        if is_new:
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

    if parsed.category:
        links.append(("domain", parsed.category, "concerns"))

    for link in parsed.wikilinks:
        links.append(("note", link, "concerns"))

    return links


# ---------------------------------------------------------------------------
# Fold-ins: `Someday.md` and `Delegated Tasks.md` / `Delegated Tasks - Done.md`
# ---------------------------------------------------------------------------

SOMEDAY_SOURCE = "someday_import"
DELEGATED_SOURCE = "delegated_import"


def _import_grouped(
    session: Session,
    content: str,
    *,
    default_status: str,
    source: str,
    done_event_note: str,
    person_link: bool,
    owner_id: int | None = None,
    dry_run: bool = False,
) -> ImportResult:
    """Shared body for `import_someday` / `import_delegated_*`."""
    result = ImportResult()
    parsed_tasks = parse_grouped_list(content)
    result.parsed = len(parsed_tasks)
    next_seq = _next_uid(session, Task, "TASK")

    for parsed in parsed_tasks:
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
            task.owner_id = owner_id
        else:
            result.tasks_updated += 1

        title, overflow = split_title(parsed.title)
        task.title = title
        task.description = "\n\n".join(
            part for part in (overflow, parsed.description) if part
        ) or None
        task.status = "done" if parsed.completed else default_status
        task.priority = parsed.priority
        task.tags = parsed.tags
        task.sort_order = parsed.order
        task.context = parsed.context
        task.energy = parsed.energy
        task.due_at = _as_utc(parsed.due_date)
        task.completed_at = _as_utc(parsed.done_date)
        task.source = source
        if is_new:
            task.confirmed_at = datetime.now(timezone.utc)
        session.flush()

        if is_new:
            if parsed.completed and parsed.done_date:
                session.add(TaskEvent(
                    task_id=task.id, from_status=None, to_status="done",
                    at=_as_utc(parsed.done_date), note=done_event_note,
                ))
                result.events_created += 1

            links: list[tuple[str, str, str]] = []
            if parsed.section:
                if person_link:
                    links.append(("person", parsed.section, "waiting_on"))
                else:
                    links.append(("domain", parsed.section, "concerns"))
            for link in parsed.wikilinks:
                links.append(("note", link, "concerns"))
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


def import_someday(
    session: Session, content: str, *, owner_id: int | None = None, dry_run: bool = False,
) -> ImportResult:
    """Import `Someday.md`."""
    return _import_grouped(
        session, content,
        default_status="someday", source=SOMEDAY_SOURCE,
        done_event_note="imported from Someday.md",
        person_link=False, owner_id=owner_id, dry_run=dry_run,
    )


def import_delegated(
    session: Session, content: str, *, owner_id: int | None = None, dry_run: bool = False,
) -> ImportResult:
    """Import the open half of `Delegated Tasks.md`."""
    return _import_grouped(
        session, content,
        default_status="waiting", source=DELEGATED_SOURCE,
        done_event_note="imported from Delegated Tasks.md",
        person_link=True, owner_id=owner_id, dry_run=dry_run,
    )


def import_delegated_done(
    session: Session, content: str, *, owner_id: int | None = None, dry_run: bool = False,
) -> ImportResult:
    """Import `Delegated Tasks - Done.md`."""
    return _import_grouped(
        session, content,
        default_status="done", source=DELEGATED_SOURCE,
        done_event_note="imported from Delegated Tasks - Done.md",
        person_link=True, owner_id=owner_id, dry_run=dry_run,
    )
