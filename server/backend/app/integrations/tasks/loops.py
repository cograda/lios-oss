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

Routines — the recurring class — landed 2026-09-03 (chunk E3) in
`routines.py`, not here: a routine is a template, a round is one occurrence
and IS a `tasks` row (`Task.routine_id`), and routines take domain tags via
the same `TaskDomainTag` table this module owns (`set_domain_tags`/
`domain_names` below now accept a fourth target, `routine=`). Routines may
also hang off a program or project via their own nullable FKs, same as a
project's `program_id`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
# Through the facade, never `household.models` — the capability-boundary test
# forbids the raw import, and the facade grew this consumer's lookups for it.
from app.integrations.household.facade import FACADE as household
from app.integrations.tasks.models import (
    PROGRAM_STATUSES, PROJECT_STATUSES, Routine, Task, TaskDomainTag, TaskEvent,
    TaskLink, TaskProgram, TaskProject,
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
    routine: Routine | None = None,
) -> list[str]:
    """Replace the domain tags on exactly one target."""
    targets = [x for x in (program, project, task, routine) if x is not None]
    assert len(targets) == 1, "exactly one target"
    domains = domains_by_name(session, names)
    q = session.query(TaskDomainTag)
    if program is not None:
        q = q.filter(TaskDomainTag.program_id == program.id)
    elif project is not None:
        q = q.filter(TaskDomainTag.project_id == project.id)
    elif routine is not None:
        q = q.filter(TaskDomainTag.routine_id == routine.id)
    else:
        q = q.filter(TaskDomainTag.task_id == task.id)
    q.delete(synchronize_session=False)
    for d in domains:
        session.add(TaskDomainTag(
            domain_id=d.id,
            program_id=program.id if program else None,
            project_id=project.id if project else None,
            task_id=task.id if task else None,
            routine_id=routine.id if routine else None,
        ))
    session.flush()
    return [d.name for d in domains]


def domain_names(session: Session) -> dict[tuple[str, int], list[str]]:
    """(kind, id) → domain names, one query for a whole render or listing.

    Two sources, merged here and nowhere else, so every consumer — the
    `domain=` filter, row serialisation, `tasks_structure`, the routines
    listing — sees the same answer:

    1. **Explicit rows** in `task_domain_tags`, on any of the four targets.
    2. **A free tag whose name matches a domain**, case-insensitively:
       `#admin` → Admin, `#home` → Home. Tasks only, because only tasks carry
       free tags. Found 2026-09-07: 65 open tasks carried `#admin`, 32
       `#home`, 31 `#kids`, and *none* of them counted as in that domain,
       because the file's tags and the loops layer's domains had never been
       joined. Resolution, not migration — no rows are written, so a domain
       renamed or added later re-resolves on the next call, and a tag that
       matches no domain resolves to nothing rather than inventing one.

    Explicit names come first, then tag-derived, deduplicated; the same
    domain named both ways is one name.
    """
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
        elif tag.routine_id is not None:
            out.setdefault(("routine", tag.routine_id), []).append(name)

    by_lower = {name.lower(): name for name in names.values()}
    if by_lower:
        tagged = session.query(Task.id, Task.tags).filter(func.cardinality(Task.tags) > 0).all()
        for task_id, task_tags in tagged:
            derived = sorted(
                {by_lower[t] for t in (_domain_key(tag) for tag in task_tags or []) if t in by_lower}
            )
            if not derived:
                continue
            have = out.setdefault(("task", task_id), [])
            have.extend(n for n in derived if n not in have)
    return out


def _domain_key(tag: str) -> str:
    """`#Admin` / `admin` → `admin`; the part of a tag that names a domain.
    Namespaced tags (`#claude/do`, `#person/isla`) keep their slash and so
    never collide with a domain name."""
    return tag.strip().lstrip("#").lower()


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


def _open_task_counts(session: Session, owner_id: int | None = None) -> dict[int, int]:
    """project_id → open task count; the caller's own loops only when
    `owner_id` is given."""
    q = (
        session.query(Task.project_id, func.count(Task.id))
        .filter(Task.status.in_(OPEN_TASK_STATUSES), Task.project_id.isnot(None))
    )
    if owner_id is not None:
        q = q.filter(Task.owner_id == owner_id)
    rows = q.group_by(Task.project_id).all()
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
    scope = args.get("scope") or "mine"
    if scope not in ("mine", "household"):
        raise ValueError("scope must be 'mine' or 'household'")
    # `mine` (default): the caller's hierarchy — programs and projects they
    # own, plus any they do not own that hold one of their open loops (a loop
    # shared into someone else's project shows that project, with only the
    # caller's count). Counts are the caller's loops, never the household's.
    # 2026-09-06: the rail showed Sam every one of Alex's programs with his
    # counts; the ledger is household-shared by design, so the scoping has to
    # happen here, per caller, like tasks_query's `owner`.
    me = current_user_id() if scope == "mine" else None
    dnames = domain_names(session)
    counts = _open_task_counts(session, owner_id=me)
    programs = session.query(TaskProgram).order_by(TaskProgram.sort_order, TaskProgram.id).all()
    projects = session.query(TaskProject).order_by(TaskProject.sort_order, TaskProject.id).all()
    if not include_closed:
        programs = [p for p in programs if p.status in OPEN_LOOP_STATUSES]
        projects = [p for p in projects if p.status in OPEN_LOOP_STATUSES]
    if me is not None:
        projects = [pj for pj in projects if pj.owner_id == me or counts.get(pj.id, 0) > 0]
        visible_program_ids = {pj.program_id for pj in projects if pj.program_id is not None}
        programs = [p for p in programs if p.owner_id == me or p.id in visible_program_ids]
    by_program: dict[int | None, list[TaskProject]] = {}
    for pj in projects:
        by_program.setdefault(pj.program_id, []).append(pj)

    unfiled_q = session.query(Task).filter(
        Task.status.in_(OPEN_TASK_STATUSES), Task.project_id.is_(None),
    )
    if me is not None:
        unfiled_q = unfiled_q.filter(Task.owner_id == me)
    unfiled = unfiled_q.count()

    domain_list = [d.name for d in household.list_domains(session)]
    return json.dumps({
        "programs": [_program_row(p, dnames, by_program.get(p.id, []), counts) for p in programs],
        # taskdb's "ad hoc": legitimate, and a signal when it clusters.
        "projects_without_program": [_project_row(pj, dnames, counts) for pj in by_program.get(None, [])],
        "tasks_without_project": unfiled,
        "domains": domain_list,
        "domain_counts": _open_domain_counts(session, dnames, domain_list, owner_id=me),
    })


def _open_domain_counts(
    session: Session, dnames: dict, domain_list: list[str], *, owner_id: int | None,
) -> dict[str, int]:
    """domain name → open task count, every domain present (0 where none).

    A task counts in a domain the same way `tasks_query`'s `domain=` filter
    and each row's `domains` list decide it: its own domains (explicit rows
    OR a matching free tag — see `domain_names`), plus those inherited from
    its project and program, each task counted once per domain. Scoped to
    the caller's loops when `owner_id` is given, like every other count here.
    """
    q = session.query(Task.id, Task.project_id).filter(Task.status.in_(OPEN_TASK_STATUSES))
    if owner_id is not None:
        q = q.filter(Task.owner_id == owner_id)
    program_of = dict(session.query(TaskProject.id, TaskProject.program_id).all())
    counts = {name: 0 for name in domain_list}
    for task_id, project_id in q.all():
        seen = set(dnames.get(("task", task_id), []))
        if project_id is not None:
            seen |= set(dnames.get(("project", project_id), []))
            program_id = program_of.get(project_id)
            if program_id is not None:
                seen |= set(dnames.get(("program", program_id), []))
        for name in seen:
            counts[name] = counts.get(name, 0) + 1
    return counts


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
    return json.dumps(
        {action: _program_row(program, dnames, projects, _open_task_counts(session))}
    )


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
    return json.dumps(
        {action: _project_row(project, domain_names(session), _open_task_counts(session))}
    )


def tasks_history_handler(session: Session, args: dict) -> str:
    """What changed, field by field. Separates work that got done from work
    that got tidied — a week of merges and re-prioritising is a week of admin,
    and saying so is more useful than a change count."""
    from app.integrations.tasks.tools import _scope_owner  # noqa: PLC0415 - avoid import cycle

    q = session.query(TaskEvent, Task.uid, Task.title).outerjoin(Task, Task.id == TaskEvent.task_id)
    if args.get("since"):
        q = q.filter(TaskEvent.at >= args["since"])
    if args.get("uid"):
        # An explicit uid is an explicit ask: the ledger is shared, so one
        # task's history is readable whoever owns it (2026-09-06 audit).
        q = q.filter(Task.uid == args["uid"])
    else:
        # The unscoped FEED is the caller's own tasks' events, like every
        # other read tool; `owner="household"` is everyone's, which — being
        # unfiltered — also keeps the task-less rows (sweeps, renders,
        # routine-level events) that belong to nobody's task.
        owner_id = _scope_owner(session, args)
        if owner_id is not None:
            q = q.filter(Task.owner_id == owner_id)
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
                "the count of open tasks filed under no project. domain_counts is "
                "open tasks per domain — a task is in a domain via an explicit "
                "domain tag OR a free tag matching the domain's name (#admin → "
                "Admin), on the task, its project or its program. scope='mine' "
                "(default) is the caller's hierarchy: programs and projects they "
                "own or that hold one of their open loops, with their own counts; "
                "scope='household' is everyone's."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_closed": {"type": "boolean", "default": False},
                    "scope": {"type": "string", "enum": ["mine", "household"], "default": "mine"},
                },
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
                    "uid": {"type": "string", "description": "One task's history, whoever owns it."},
                    "owner": {"type": "string", "description": "Defaults to you. 'household' for everyone's; else 'me', a user id, or a users.name."},
                    "limit": {"type": "integer", "default": 200, "maximum": 1000},
                },
            },
            handler=tasks_history_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
    ]
