"""The loops layer over the task ledger: programs, domain tags, containment.

Decided with Alex on 2026-09-01, reading the work `taskdb` export against what
had shipped on 2026-08-31:

- **domains → programs → projects → tasks.** Program is the level above
  project; it has a hub note and a *nullable* definition of done.
- **A loop is anything with a definition of done.** It can be closed, and it
  may contain sub-loops that must close first. Programs *may* have one;
  projects *must*; tasks *must* (a task's is its own text). The closing rule
  is enforced here: a program cannot complete over an open project, a project
  over an open task, a task over an open `part_of` child.
- **Domains are tags, many-to-many**, on any of the three levels — not a
  single value on a program. `domains` itself belongs to the household
  integration (areas of responsibility with an owner and an operational
  definition); this module references those rows and never creates them.

Routines — the recurring class — are NOT here yet. `household.Domain`'s
cadence + checklist + `DomainCheck` is already a third of one, and deciding
how routines hang off programs is the next design step, not this change.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.auth.context import current_user_id
# Through the facade, never `household.models` — the capability-boundary test
# forbids the raw import, and the facade grew this consumer's lookups for it.
from app.integrations.household.facade import FACADE as household
from app.integrations.tasks.models import (
    PROGRAM_STATUSES, PROJECT_STATUSES, Task, TaskDomainTag, TaskEvent, TaskLink,
    TaskProgram, TaskProject,
)
from app.services.text import escape_ilike
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import iso_or_none

PART_OF = "part_of"
OPEN_TASK_STATUSES = ("inbox", "next", "waiting", "scheduled")
OPEN_LOOP_STATUSES = ("active", "someday")


# ─── uids ──────────────────────────────────────────────────────────────────


def next_uid(session: Session, model, prefix: str) -> str:
    rows = session.query(model.uid).filter(model.uid.like(f"{prefix}-%")).all()
    nums = [int(r[0].split("-")[1]) for r in rows if r[0].split("-")[1].isdigit()]
    return f"{prefix}-{max(nums, default=0) + 1:04d}"


# ─── domains ───────────────────────────────────────────────────────────────


def domains_by_name(session: Session, names: list[str]) -> list:
    """Resolve domain names, case-insensitively. Unknown names are refused
    *with the list that exists* — the same shape as an unknown project, and
    for the same reason: the caller can fix it without a second round trip.
    Creating a domain is `household_domain_add`; it needs an owner and an
    operational definition, which a task tag has no business inventing."""
    if not names:
        return []
    existing = household.list_domains(session)
    by_lower = {d.name.lower(): d for d in existing}
    missing = [n for n in names if n.lower() not in by_lower]
    if missing:
        raise ValueError(
            f"Unknown domain(s): {', '.join(missing)}. Existing domains: "
            + (", ".join(sorted(d.name for d in existing)) or "(none — create one with household_domain_add)")
        )
    return [by_lower[n.lower()] for n in names]


def set_domain_tags(
    session: Session, names: list[str], *,
    program: TaskProgram | None = None,
    project: TaskProject | None = None,
    task: Task | None = None,
) -> list[str]:
    """Replace the domain tags on exactly one target."""
    targets = [x for x in (program, project, task) if x is not None]
    assert len(targets) == 1, "exactly one target"
    domains = domains_by_name(session, names)
    q = session.query(TaskDomainTag)
    if program is not None:
        q = q.filter(TaskDomainTag.program_id == program.id)
    elif project is not None:
        q = q.filter(TaskDomainTag.project_id == project.id)
    else:
        q = q.filter(TaskDomainTag.task_id == task.id)
    q.delete(synchronize_session=False)
    for d in domains:
        session.add(TaskDomainTag(
            domain_id=d.id,
            program_id=program.id if program else None,
            project_id=project.id if project else None,
            task_id=task.id if task else None,
        ))
    session.flush()
    return [d.name for d in domains]


def domain_names(session: Session) -> dict[tuple[str, int], list[str]]:
    """(kind, id) → domain names, one query for a whole render or listing."""
    out: dict[tuple[str, int], list[str]] = {}
    names = household.domain_names_by_id(session)
    tags = session.query(TaskDomainTag).all()
    for tag in sorted(tags, key=lambda t: names.get(t.domain_id, "")):
        name = names.get(tag.domain_id)
        if name is None:
            continue
        if tag.program_id is not None:
            out.setdefault(("program", tag.program_id), []).append(name)
        elif tag.project_id is not None:
            out.setdefault(("project", tag.project_id), []).append(name)
        elif tag.task_id is not None:
            out.setdefault(("task", tag.task_id), []).append(name)
    return out


# ─── containment ───────────────────────────────────────────────────────────


def open_children(session: Session, task: Task) -> list[str]:
    """uids of open tasks that are `part_of` this one."""
    rows = (
        session.query(Task.uid)
        .join(TaskLink, TaskLink.from_task_id == Task.id)
        .filter(
            TaskLink.predicate == PART_OF,
            TaskLink.target_type == "task",
            TaskLink.target_ref == task.uid,
            Task.status.in_(OPEN_TASK_STATUSES),
        )
        .all()
    )
    return sorted(r[0] for r in rows)


def parent_of(session: Session, task: Task) -> str | None:
    link = (
        session.query(TaskLink)
        .filter(TaskLink.from_task_id == task.id, TaskLink.predicate == PART_OF)
        .one_or_none()
    )
    return link.target_ref if link else None


def set_parent(session: Session, task: Task, parent_uid: str | None) -> None:
    """Make `task` a sub-loop of `parent_uid`, or of nothing.

    Cycles are refused by walking up from the proposed parent: if `task` is
    already an ancestor of it, the containment would have no top and nothing
    in it could ever close.
    """
    session.query(TaskLink).filter(
        TaskLink.from_task_id == task.id, TaskLink.predicate == PART_OF,
    ).delete(synchronize_session=False)
    if parent_uid is None:
        return
    if parent_uid == task.uid:
        raise ValueError("A task cannot be part of itself.")
    parent = session.query(Task).filter(Task.uid == parent_uid).one_or_none()
    if parent is None:
        raise ValueError(f"Unknown task uid: {parent_uid}")

    node, seen = parent, set()
    while node is not None and node.uid not in seen:
        seen.add(node.uid)
        up = parent_of(session, node)
        if up == task.uid:
            raise ValueError(
                f"{parent_uid} is already inside {task.uid}, directly or through "
                f"another task. Making it the parent would be a cycle."
            )
        node = session.query(Task).filter(Task.uid == up).one_or_none() if up else None

    session.add(TaskLink(
        from_task_id=task.id, target_type="task", target_ref=parent.uid,
        predicate=PART_OF, confidence=1.0, derived_by="human",
    ))
    session.flush()


# ─── programs and projects ─────────────────────────────────────────────────


def _program_row(p: TaskProgram, dnames: dict, projects: list[TaskProject], open_counts: dict) -> dict:
    return {
        "uid": p.uid, "title": p.title, "status": p.status,
        "done_when": p.done_when, "note_path": p.note_path,
        "description": p.description,
        "domains": dnames.get(("program", p.id), []),
        "projects": [_project_row(pj, dnames, open_counts) for pj in projects],
        "open_tasks": sum(open_counts.get(pj.id, 0) for pj in projects),
    }


def _project_row(pj: TaskProject, dnames: dict, open_counts: dict) -> dict:
    return {
        "uid": pj.uid, "title": pj.title, "status": pj.status,
        "done_when": pj.outcome, "note_path": pj.note_path,
        "domains": dnames.get(("project", pj.id), []),
        "open_tasks": open_counts.get(pj.id, 0),
    }


def _open_task_counts(session: Session) -> dict[int, int]:
    from sqlalchemy import func
    rows = (
        session.query(Task.project_id, func.count(Task.id))
        .filter(Task.status.in_(OPEN_TASK_STATUSES), Task.project_id.isnot(None))
        .group_by(Task.project_id)
        .all()
    )
    return {pid: n for pid, n in rows}


def _find_program(session: Session, ref: str) -> TaskProgram:
    p = session.query(TaskProgram).filter(
        (TaskProgram.uid == ref) | (TaskProgram.title.ilike(escape_ilike(ref)))
    ).one_or_none()
    if p is None:
        raise ValueError(
            f"Unknown program: {ref}. Existing programs: "
            + (", ".join(sorted(x.title for x in session.query(TaskProgram).all())) or "(none)")
        )
    return p


def _find_project(session: Session, ref: str) -> TaskProject:
    p = session.query(TaskProject).filter(
        (TaskProject.uid == ref) | (TaskProject.title.ilike(escape_ilike(ref)))
    ).one_or_none()
    if p is None:
        raise ValueError(
            f"Unknown project: {ref}. Existing projects: "
            + ", ".join(sorted(x.title for x in session.query(TaskProject).all()))
        )
    return p


def tasks_structure_handler(session: Session, args: dict) -> str:
    """Read-only: the whole hierarchy with its definitions of done, so a review
    can check each program's end condition against the work under it — the
    highest-value check in taskdb's placement pass."""
    include_closed = bool(args.get("include_closed"))
    dnames = domain_names(session)
    counts = _open_task_counts(session)
    programs = session.query(TaskProgram).order_by(TaskProgram.sort_order, TaskProgram.id).all()
    projects = session.query(TaskProject).order_by(TaskProject.sort_order, TaskProject.id).all()
    if not include_closed:
        programs = [p for p in programs if p.status in OPEN_LOOP_STATUSES]
        projects = [p for p in projects if p.status in OPEN_LOOP_STATUSES]
    by_program: dict[int | None, list[TaskProject]] = {}
    for pj in projects:
        by_program.setdefault(pj.program_id, []).append(pj)

    unfiled = session.query(Task).filter(
        Task.status.in_(OPEN_TASK_STATUSES), Task.project_id.is_(None),
    ).count()

    return json.dumps({
        "programs": [_program_row(p, dnames, by_program.get(p.id, []), counts) for p in programs],
        # taskdb's "ad hoc": legitimate, and a signal when it clusters.
        "projects_without_program": [_project_row(pj, dnames, counts) for pj in by_program.get(None, [])],
        "tasks_without_project": unfiled,
        "domains": [d.name for d in household.list_domains(session)],
    })


def _complete_loop(session: Session, *, kind: str, title: str, open_items: list[str]) -> None:
    if open_items:
        raise ValueError(
            f"{kind} '{title}' has open {'projects' if kind == 'Program' else 'tasks'} and "
            f"cannot close until they do: {', '.join(open_items[:8])}"
            + (f" (+{len(open_items) - 8} more)" if len(open_items) > 8 else "")
        )


def tasks_program_handler(session: Session, args: dict) -> str:
    action = args.get("action", "add")
    now = datetime.now(timezone.utc)
    if action == "add":
        title = args["title"].strip()
        if session.query(TaskProgram).filter(TaskProgram.title.ilike(escape_ilike(title))).first():
            raise ValueError(f"A program called '{title}' already exists.")
        program = TaskProgram(
            uid=next_uid(session, TaskProgram, "PROG"), title=title[:300],
            description=args.get("description"), note_path=args.get("note_path"),
            done_when=args.get("done_when"), status=args.get("status", "active"),
            owner_id=current_user_id(),
            sort_order=(session.query(TaskProgram).count() + 1) * 1000,
        )
        session.add(program)
        session.flush()
    elif action == "update":
        program = _find_program(session, args["program"])
        for field in ("title", "description", "note_path", "done_when"):
            if field in args:
                setattr(program, field, args[field])
        if "status" in args:
            status = args["status"]
            if status not in PROGRAM_STATUSES:
                raise ValueError(f"status must be one of {PROGRAM_STATUSES}")
            if status == "complete" and program.status != "complete":
                open_projects = [
                    pj.title for pj in session.query(TaskProject)
                    .filter(TaskProject.program_id == program.id, TaskProject.status.in_(OPEN_LOOP_STATUSES))
                ]
                _complete_loop(session, kind="Program", title=program.title, open_items=open_projects)
                program.completed_at = now
            if status != "complete":
                program.completed_at = None
            program.status = status
    else:
        raise ValueError(f"Unknown action: {action}")

    if "domains" in args:
        set_domain_tags(session, args["domains"] or [], program=program)
    if "projects" in args:
        for ref in args["projects"] or []:
            _find_project(session, ref).program_id = program.id

    session.commit()
    from app.integrations.tasks.tools import _render  # noqa: PLC0415 - avoid import cycle
    _render(session)
    dnames = domain_names(session)
    projects = session.query(TaskProject).filter(TaskProject.program_id == program.id).all()
    return json.dumps({action: _program_row(program, dnames, projects, _open_task_counts(session))})


def tasks_project_handler(session: Session, args: dict) -> str:
    """Create or update a project. Under the loops rule a project MUST have a
    definition of done, so `add` requires `done_when` — taskdb's rule: if you
    cannot write one, it is a theme (standing work) or a program, not a
    project."""
    action = args.get("action", "add")
    now = datetime.now(timezone.utc)
    if action == "add":
        title = args["title"].strip()
        if not (args.get("done_when") or "").strip():
            raise ValueError(
                "A project needs a definition of done (`done_when`). If you cannot "
                "write one, it is standing work or a program, not a project."
            )
        if session.query(TaskProject).filter(TaskProject.title.ilike(escape_ilike(title))).first():
            raise ValueError(f"A project called '{title}' already exists.")
        project = TaskProject(
            uid=next_uid(session, TaskProject, "PROJ"), title=title[:300],
            description=args.get("description"), note_path=args.get("note_path"),
            outcome=args["done_when"], status=args.get("status", "active"),
            owner_id=current_user_id(),
            sort_order=(session.query(TaskProject).count() + 1) * 1000,
        )
        session.add(project)
        session.flush()
    elif action == "update":
        project = _find_project(session, args["project"])
        for field in ("title", "description", "note_path"):
            if field in args:
                setattr(project, field, args[field])
        if "done_when" in args:
            project.outcome = args["done_when"]
        if "status" in args:
            status = args["status"]
            if status not in PROJECT_STATUSES:
                raise ValueError(f"status must be one of {PROJECT_STATUSES}")
            if status == "complete" and project.status != "complete":
                open_tasks = [
                    t.uid for t in session.query(Task)
                    .filter(Task.project_id == project.id, Task.status.in_(OPEN_TASK_STATUSES))
                ]
                _complete_loop(session, kind="Project", title=project.title, open_items=open_tasks)
                project.completed_at = now
            if status != "complete":
                project.completed_at = None
            project.status = status
    else:
        raise ValueError(f"Unknown action: {action}")

    if "program" in args:
        project.program_id = None if args["program"] is None else _find_program(session, args["program"]).id
    if "domains" in args:
        set_domain_tags(session, args["domains"] or [], project=project)

    session.commit()
    from app.integrations.tasks.tools import _render  # noqa: PLC0415
    _render(session)
    return json.dumps({action: _project_row(project, domain_names(session), _open_task_counts(session))})


def tasks_history_handler(session: Session, args: dict) -> str:
    """What changed, field by field. Separates work that got done from work
    that got tidied — a week of merges and re-prioritising is a week of admin,
    and saying so is more useful than a change count."""
    q = session.query(TaskEvent, Task.uid, Task.title).outerjoin(Task, Task.id == TaskEvent.task_id)
    if args.get("since"):
        q = q.filter(TaskEvent.at >= args["since"])
    if args.get("uid"):
        q = q.filter(Task.uid == args["uid"])
    limit = min(int(args.get("limit", 200)), 1000)
    rows = q.order_by(TaskEvent.at.desc()).limit(limit).all()
    events = []
    for ev, uid, title in rows:
        events.append({
            "at": iso_or_none(ev.at), "uid": uid, "title": title,
            "kind": "status" if ev.field is None else "field",
            "field": ev.field or "status",
            "from": ev.old_value if ev.field else ev.from_status,
            "to": ev.new_value if ev.field else ev.to_status,
            "actor_id": ev.actor_id, "note": ev.note,
        })
    done = sum(1 for e in events if e["field"] == "status" and e["to"] == "done")
    tidied = sum(1 for e in events if e["field"] != "status")
    return json.dumps({"count": len(events), "completed": done, "tidied": tidied, "events": events})


def loops_tools() -> list[dict]:
    _domains = {
        "type": "array", "items": {"type": "string"},
        "description": "Domain names (household areas). Replaces the current set; must already exist.",
    }
    return [
        CustomTool(
            name="tasks_structure",
            description=(
                "The hierarchy: domains → programs → projects → open task counts, "
                "each with its definition of done. Read-only. Use it to check a "
                "program's end condition against the work under it, to find "
                "projects with no program (ad hoc — fine until they cluster), and "
                "the count of open tasks filed under no project."
            ),
            input_schema={
                "type": "object",
                "properties": {"include_closed": {"type": "boolean", "default": False}},
            },
            handler=tasks_structure_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_program",
            description=(
                "Add or update a program — the level above project. A program has "
                "a hub note and MAY have a definition of done (done_when); leave it "
                "empty for standing work. Domains are many-to-many tags. Assign "
                "projects to it by title. Completing a program is refused while "
                "any of its projects is open. Re-renders Task Backlog.md."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "update"], "default": "add"},
                    "program": {"type": "string", "description": "update: existing title or uid (PROG-0001)."},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "note_path": {"type": "string", "description": "Vault path of the hub note."},
                    "done_when": {"type": "string", "description": "Nullable — what has to be true for it to be over."},
                    "status": {"type": "string", "enum": list(PROGRAM_STATUSES)},
                    "domains": _domains,
                    "projects": {"type": "array", "items": {"type": "string"}, "description": "Project titles to file under this program."},
                },
            },
            handler=tasks_program_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_project",
            description=(
                "Add or update a project. A project MUST have a definition of done "
                "(done_when) — if you cannot write one it is standing work or a "
                "program. Optionally file it under a program and tag domains. "
                "Completing a project is refused while any of its tasks is open. "
                "Re-renders Task Backlog.md."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "update"], "default": "add"},
                    "project": {"type": "string", "description": "update: existing title or uid."},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "note_path": {"type": "string"},
                    "done_when": {"type": "string"},
                    "status": {"type": "string", "enum": list(PROJECT_STATUSES)},
                    "program": {"type": ["string", "null"], "description": "Program title or uid; null to unfile."},
                    "domains": _domains,
                },
            },
            handler=tasks_project_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_history",
            description=(
                "Field-level change history for the ledger: status transitions and "
                "every other edit (priority, queue, project, title, …), newest "
                "first. Reports how many events were completions versus tidying, "
                "so a week review can say which kind of week it was."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {"type": "string", "format": "date-time"},
                    "uid": {"type": "string"},
                    "limit": {"type": "integer", "default": 200, "maximum": 1000},
                },
            },
            handler=tasks_history_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
    ]
