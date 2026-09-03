"""MCP tools for the task ledger.

The database is the source of truth and `Task Backlog.md` is a one-way view,
so **every write here re-renders the file**. That is not a nicety: from the
moment the render shipped, a change that does not re-render is a change
nobody can see.

  - tasks_query:       read the ledger — the lenses the file's ```tasks blocks
                       express, as SQL
  - tasks_add:         a new task
  - tasks_update:      change one task by uid
  - tasks_complete:    mark done (its own tool because it is the common case
                       and it must write the event)
  - tasks_bulk_update: the motion /backlog-sweep needs — many uids, one call
  - tasks_split:       one compound line → several single actions
  - tasks_review:      which lines are not actually next actions
  - tasks_block:       record / clear / inspect what waits on what
  - tasks_structure / tasks_program / tasks_project / tasks_history:
                       the loops layer (domains → programs → projects → tasks,
                       definitions of done, field-level history) — see loops.py
  - tasks_duplicates / tasks_merge:
                       pairs that look like the same task, and folding one into
                       the other — see dupes.py

🔑 **These are reachable over REST as well as MCP**, with no extra work:
`POST /api/v1/tools/{name}` dispatches every registered tool through the same
chokepoint, so a browser client and a model client cannot drift apart. That
is the projection property the platform plan wants, arriving early.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.tasks import blocking, dupes, loops
from app.integrations.tasks.models import (
    PRIORITIES, QUEUES, TASK_STATUSES, Task, TaskEvent, TaskProgram, TaskProject,
)
from app.integrations.tasks.render import check_drift, write_backlog_note
from app.integrations.tasks.review import FLAGS, flags_for, summarise
from app.services.text import escape_ilike
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import iso_or_none, serialize

logger = logging.getLogger(__name__)

_ROW_FIELDS = [
    "uid", "title", "status", "priority", "context", "energy",
    "estimate_min", "source", "description",
]

OPEN_STATUSES = ("inbox", "next", "waiting", "scheduled")


def _row(
    task: Task, project: TaskProject | None = None, *,
    program: TaskProgram | None = None, domains: list[str] | None = None,
) -> dict:
    out = serialize(task, _ROW_FIELDS)
    out["due_at"] = iso_or_none(task.due_at)
    out["defer_until"] = iso_or_none(task.defer_until)
    out["completed_at"] = iso_or_none(task.completed_at)
    out["created_at"] = iso_or_none(task.created_at)
    out["tags"] = list(task.tags or [])
    out["project"] = project.title if project else None
    out["program"] = program.title if program else None
    out["queue"] = task.queue
    out["queue_set_at"] = iso_or_none(task.queue_set_at)
    out["domains"] = domains or []
    return out


def _rows(session: Session, tasks: list[Task]) -> list[dict]:
    """Rows with their project, program and domain tags — three queries for
    the whole list rather than three per task. A task's domains are its own
    tags plus those inherited from its project and program, deduplicated."""
    projects = {p.id: p for p in session.query(TaskProject).all()}
    programs = {p.id: p for p in session.query(TaskProgram).all()}
    dnames = loops.domain_names(session)
    out = []
    for t in tasks:
        project = projects.get(t.project_id) if t.project_id else None
        program = programs.get(project.program_id) if project and project.program_id else None
        seen: list[str] = []
        for key in (("task", t.id), ("project", project.id if project else -1), ("program", program.id if program else -1)):
            for name in dnames.get(key, []):
                if name not in seen:
                    seen.append(name)
        out.append(_row(t, project, program=program, domains=seen))
    return out


def _render(session: Session) -> None:
    """Re-render the vault view. Never swallowed silently — if the file cannot
    be written the caller needs to know the ledger and the file now disagree."""
    write_backlog_note(session)


def _get(session: Session, uid: str) -> Task:
    task = session.query(Task).filter(Task.uid == uid).one_or_none()
    if task is None:
        raise ValueError(f"Unknown task uid: {uid}")
    return task


def _next_uid(session: Session) -> str:
    rows = session.query(Task.uid).filter(Task.uid.like("TASK-%")).all()
    nums = [int(r[0].split("-")[1]) for r in rows if r[0].split("-")[1].isdigit()]
    return f"TASK-{max(nums, default=0) + 1:04d}"


def _set_status(session: Session, task: Task, new_status: str, note: str | None = None) -> None:
    """Change status and record the transition.

    The event is the whole point of the table: it is the only thing that can
    answer what has been open longest or what drained last month, and those
    are unanswerable for everything that predates the ledger.
    """
    if new_status == task.status:
        return
    if new_status == "done":
        # The loops rule: a loop closes only when its sub-loops have.
        children = loops.open_children(session, task)
        if children:
            raise ValueError(
                f"{task.uid} has open sub-tasks and cannot complete until they do: "
                + ", ".join(children)
            )
    session.add(TaskEvent(
        task_id=task.id, from_status=task.status, to_status=new_status,
        actor_id=current_user_id(), note=note,
    ))
    task.status = new_status
    if new_status == "done" and task.completed_at is None:
        task.completed_at = datetime.now(timezone.utc)
    if new_status != "done":
        task.completed_at = None


# ─── handlers ──────────────────────────────────────────────────────────────


def tasks_query_handler(session: Session, args: dict) -> str:
    q = session.query(Task)

    status = args.get("status")
    if status:
        q = q.filter(Task.status == status)
    elif not args.get("include_done"):
        q = q.filter(Task.status.in_(OPEN_STATUSES))

    if args.get("priority"):
        q = q.filter(Task.priority == args["priority"])
    if args.get("context"):
        q = q.filter(Task.context == args["context"])
    if args.get("energy"):
        q = q.filter(Task.energy == args["energy"])
    if args.get("tag"):
        q = q.filter(Task.tags.any(args["tag"]))
    if args.get("project"):
        project_ids = [
            p.id for p in session.query(TaskProject)
            .filter(TaskProject.title.ilike(f"%{escape_ilike(args['project'])}%")).all()
        ]
        q = q.filter(Task.project_id.in_(project_ids or [-1]))
    if args.get("program"):
        program_ids = [
            p.id for p in session.query(TaskProgram)
            .filter(TaskProgram.title.ilike(f"%{escape_ilike(args['program'])}%")).all()
        ]
        project_ids = [
            p.id for p in session.query(TaskProject)
            .filter(TaskProject.program_id.in_(program_ids or [-1])).all()
        ]
        q = q.filter(Task.project_id.in_(project_ids or [-1]))
    if args.get("queue"):
        # Focus is a subset of week, stored as one value: asking for the week
        # returns focus too; asking for focus returns only focus.
        wanted = ("week", "focus") if args["queue"] == "week" else ("focus",)
        q = q.filter(Task.queue.in_(wanted))
    if args.get("domain"):
        dnames = loops.domain_names(session)
        want = args["domain"].lower()
        task_ids = {tid for (kind, tid), names in dnames.items() if kind == "task" and want in map(str.lower, names)}
        project_ids = {pid for (kind, pid), names in dnames.items() if kind == "project" and want in map(str.lower, names)}
        program_ids = {pid for (kind, pid), names in dnames.items() if kind == "program" and want in map(str.lower, names)}
        if program_ids:
            project_ids |= {p.id for p in session.query(TaskProject).filter(TaskProject.program_id.in_(program_ids))}
        q = q.filter(Task.id.in_(task_ids or {-1}) | Task.project_id.in_(project_ids or {-1}))
    if args.get("text"):
        pattern = f"%{escape_ilike(args['text'])}%"
        q = q.filter(Task.title.ilike(pattern) | Task.description.ilike(pattern))
    if args.get("due_before"):
        q = q.filter(Task.due_at.isnot(None), Task.due_at <= args["due_before"])
    if args.get("overdue"):
        q = q.filter(Task.due_at.isnot(None), Task.due_at < datetime.now(timezone.utc))

    limit = min(int(args.get("limit", 50)), 500)
    tasks = q.order_by(Task.sort_order, Task.id).limit(limit).all()

    return json.dumps({"count": len(tasks), "tasks": _rows(session, tasks)})


def tasks_review_handler(session: Session, args: dict) -> str:
    """Read-only. Judges nothing — it hands a person candidates to rule on."""
    q = session.query(Task).filter(Task.status.in_(OPEN_STATUSES))
    if args.get("project"):
        ids = [
            p.id for p in session.query(TaskProject)
            .filter(TaskProject.title.ilike(f"%{escape_ilike(args['project'])}%")).all()
        ]
        q = q.filter(Task.project_id.in_(ids or [-1]))
    tasks = q.order_by(Task.sort_order, Task.id).all()

    scored = [(t, flags_for(t.title)) for t in tasks]
    wanted = args.get("flag")
    rows_by_uid = {r["uid"]: r for r in _rows(session, tasks)}

    findings = []
    for task, fs in scored:
        if not fs or (wanted and wanted not in fs):
            continue
        row = rows_by_uid[task.uid]
        row["flags"] = [{"flag": f, "why": FLAGS[f]} for f in fs]
        findings.append(row)

    out = summarise([(t.title, fs) for t, fs in scored])
    out["findings"] = findings
    # Carried here rather than in a tool of its own: this is the call every
    # client already makes on load, so the file's state arrives with no extra
    # round trip. `drifted` means someone hand-edited the generated file and
    # the next write will refuse until it is reconciled.
    out["file"] = check_drift(session)
    return json.dumps(out)


def tasks_block_handler(session: Session, args: dict) -> str:
    action = args.get("action", "list")
    if action == "add":
        blocking.add_blocker(session, args["uid"], args["blocked_by"])
    elif action == "remove":
        blocking.remove_blocker(session, args["uid"], args["blocked_by"])
    elif action != "list":
        raise ValueError(f"Unknown action: {action}")

    # Returned for every action, so a write's answer shows its effect on the
    # graph rather than just confirming itself.
    return json.dumps({
        "blocked": blocking.open_blockers(session),
        "most_blocking": blocking.most_blocking(session),
    })


def tasks_add_handler(session: Session, args: dict) -> str:
    project = None
    if args.get("project"):
        project = (
            session.query(TaskProject)
            .filter(TaskProject.title.ilike(escape_ilike(args["project"])))
            .one_or_none()
        )
        if project is None:
            raise ValueError(
                f"Unknown project: {args['project']}. Existing projects: "
                + ", ".join(sorted(p.title for p in session.query(TaskProject).all()))
            )

    # A task created here has a genuinely known creation time, unlike every
    # imported one. This is where the aging clock actually starts.
    task = Task(
        uid=_next_uid(session),
        title=args["title"][:300],
        description=args.get("description"),
        status=args.get("status", "next"),
        priority=args.get("priority"),
        project_id=project.id if project else None,
        context=args.get("context"),
        energy=args.get("energy"),
        estimate_min=args.get("estimate_min"),
        due_at=args.get("due_at"),
        defer_until=args.get("defer_until"),
        tags=args.get("tags") or [],
        source=args.get("source", "manual"),
        created_at=datetime.now(timezone.utc),
        sort_order=(session.query(Task).count() + 1) * 1000,
    )
    session.add(task)
    session.flush()
    session.add(TaskEvent(
        task_id=task.id, from_status=None, to_status=task.status,
        actor_id=current_user_id(), note="created",
    ))
    if args.get("queue"):
        _apply_update(session, task, {"queue": args["queue"]})
    if args.get("domains"):
        loops.set_domain_tags(session, args["domains"], task=task)
    dupes.enqueue(session, task)
    session.commit()
    _render(session)
    return json.dumps({"created": _rows(session, [task])[0]})


_UPDATABLE = (
    "title", "description", "priority", "context", "energy",
    "estimate_min", "due_at", "defer_until", "tags",
)


def _field_event(session: Session, task: Task, field: str, old, new) -> None:
    """One `task_events` row per changed field. `to_status` carries the
    unchanged status so the column stays NOT NULL; `field` is what marks it as
    a field change rather than a transition."""
    if old == new:
        return
    as_text = lambda v: None if v is None else (json.dumps(v) if isinstance(v, (list, dict)) else str(v))  # noqa: E731
    session.add(TaskEvent(
        task_id=task.id, from_status=None, to_status=task.status,
        actor_id=current_user_id(), field=field,
        old_value=as_text(old), new_value=as_text(new),
    ))


def _apply_update(session: Session, task: Task, args: dict) -> None:
    for field in _UPDATABLE:
        if field in args:
            _field_event(session, task, field, getattr(task, field), args[field])
            setattr(task, field, args[field])
    if "project" in args:
        if args["project"] is None:
            new_project_id = None
        else:
            project = (
                session.query(TaskProject)
                .filter(TaskProject.title.ilike(escape_ilike(args["project"])))
                .one_or_none()
            )
            if project is None:
                raise ValueError(f"Unknown project: {args['project']}")
            new_project_id = project.id
        _field_event(session, task, "project", task.project_id, new_project_id)
        task.project_id = new_project_id
    if "queue" in args:
        queue = args["queue"]
        if queue not in (None, *QUEUES):
            raise ValueError(f"queue must be one of {QUEUES} or null")
        if queue != task.queue:
            _field_event(session, task, "queue", task.queue, queue)
            task.queue = queue
            task.queue_set_at = datetime.now(timezone.utc) if queue else None
    if "domains" in args:
        before = [n for n in loops.domain_names(session).get(("task", task.id), [])]
        after = loops.set_domain_tags(session, args["domains"] or [], task=task)
        _field_event(session, task, "domains", before, after)
    if "parent" in args:
        before = loops.parent_of(session, task)
        loops.set_parent(session, task, args["parent"])
        _field_event(session, task, "parent", before, args["parent"])
    if "status" in args:
        _set_status(session, task, args["status"], note=args.get("note"))
    # Every path that changes text or status keeps the vector in step; a
    # closed task's vector is removed (see dupes.py).
    dupes.enqueue(session, task)


def tasks_update_handler(session: Session, args: dict) -> str:
    task = _get(session, args["uid"])
    _apply_update(session, task, args)
    session.commit()
    _render(session)
    return json.dumps({"updated": _rows(session, [task])[0]})


def tasks_complete_handler(session: Session, args: dict) -> str:
    task = _get(session, args["uid"])
    _set_status(session, task, "done", note=args.get("note"))
    dupes.enqueue(session, task)
    session.commit()
    _render(session)
    return json.dumps({"completed": _rows(session, [task])[0]})


def tasks_split_handler(session: Session, args: dict) -> str:
    """One compound line becomes several single actions.

    The original keeps its uid and takes the first part as its title, so
    links, history and its place in the file survive; the rest are new tasks
    that inherit everything that would otherwise have to be re-typed —
    project, priority, context, energy, tags, queue, domains, parent. This is
    what the "several actions" flag asks for, and the reason it is a tool
    rather than a UI trick: the history records a split, not a rename plus
    some unrelated additions.
    """
    task = _get(session, args["uid"])
    parts = [p.strip() for p in (args.get("parts") or []) if p and p.strip()]
    if len(parts) < 2:
        raise ValueError("split needs at least two non-empty parts")

    _field_event(session, task, "title", task.title, parts[0][:300])
    task.title = parts[0][:300]
    dupes.enqueue(session, task)

    parent = loops.parent_of(session, task)
    own_domains = loops.domain_names(session).get(("task", task.id), [])
    made = []
    base = session.query(Task).count()
    for i, part in enumerate(parts[1:], start=1):
        new = Task(
            uid=_next_uid(session),
            title=part[:300],
            status=task.status if task.status in OPEN_STATUSES else "next",
            priority=task.priority,
            project_id=task.project_id,
            context=task.context,
            energy=task.energy,
            tags=list(task.tags or []),
            source="split",
            created_at=datetime.now(timezone.utc),
            # Directly after the original, so the file keeps them together.
            sort_order=(task.sort_order or (base + i) * 1000) + i,
            queue=task.queue,
            queue_set_at=task.queue_set_at,
        )
        session.add(new)
        session.flush()
        session.add(TaskEvent(
            task_id=new.id, from_status=None, to_status=new.status,
            actor_id=current_user_id(), note=f"split from {task.uid}",
        ))
        if own_domains:
            loops.set_domain_tags(session, own_domains, task=new)
        if parent:
            loops.set_parent(session, new, parent)
        dupes.enqueue(session, new)
        made.append(new)

    session.commit()
    _render(session)
    return json.dumps({"kept": _rows(session, [task])[0], "created": _rows(session, made)})


def tasks_bulk_update_handler(session: Session, args: dict) -> str:
    """Many uids, one call — the motion /backlog-sweep needs.

    ⚠️ Renders ONCE at the end rather than per task. A sweep touching 40 tasks
    would otherwise rewrite the file 40 times, and every intermediate version
    is a state the backlog was never actually in.
    """
    updates = args["updates"]
    if not updates:
        raise ValueError("updates must not be empty")

    results, errors = [], []
    for update in updates:
        uid = update.get("uid")
        try:
            task = _get(session, uid)
            _apply_update(session, task, update)
            results.append(uid)
        except ValueError as e:
            errors.append({"uid": uid, "error": str(e)})

    # Partial success is reported, not rolled back: in a sweep, 39 good
    # changes should not be lost because one uid was mistyped.
    session.commit()
    _render(session)
    return json.dumps({"updated": results, "failed": errors})


def mcp_tools() -> list[dict[str, Any]]:
    _priority = {"type": "string", "enum": list(PRIORITIES)}
    _context = {"type": "string", "description": "GTD context, e.g. errand, sitdown, home."}
    _energy = {"type": "string", "enum": ["quick", "deep"]}
    _queue = {
        "type": ["string", "null"], "enum": [*QUEUES, None],
        "description": "Explicit work queue. 'focus' implies 'week'; null takes it out of both.",
    }
    _domains = {"type": "array", "items": {"type": "string"}, "description": "Domain names; must exist."}
    _parent = {
        "type": ["string", "null"],
        "description": "uid of the task this is a sub-loop of (part_of); null to detach. A parent cannot complete while a child is open.",
    }

    return [
        CustomTool(
            name="tasks_query",
            description=(
                "Query the household task backlog. Open tasks by default; "
                "filter by status, priority, context (errand/sitdown), energy "
                "(quick/deep), tag, project, program, queue (week/focus), "
                "domain, free text, due date or overdue. "
                "Each task has a stable uid (TASK-0042). This is the same list "
                "Task Backlog.md renders — the file is a view of this."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(TASK_STATUSES)},
                    "include_done": {"type": "boolean", "default": False},
                    "priority": _priority,
                    "context": _context,
                    "energy": _energy,
                    "tag": {"type": "string", "description": "Exact tag, e.g. '#home'."},
                    "project": {"type": "string"},
                    "program": {"type": "string", "description": "Program title (substring)."},
                    "queue": {"type": "string", "enum": list(QUEUES), "description": "'week' includes focus."},
                    "domain": {"type": "string", "description": "Domain name; matches tags on the task, its project or its program."},
                    "text": {"type": "string"},
                    "due_before": {"type": "string", "format": "date-time"},
                    "overdue": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 50, "maximum": 500},
                },
            },
            handler=tasks_query_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_add",
            description=(
                "Add a task to the household backlog. Assigns the next uid and "
                "re-renders Task Backlog.md. Unlike imported tasks, a task "
                "created here records a real created_at — the aging clock "
                "starts when a task is genuinely created, never at import."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string", "enum": list(TASK_STATUSES), "default": "next"},
                    "priority": _priority,
                    "project": {"type": "string", "description": "Existing project title."},
                    "context": _context,
                    "energy": _energy,
                    "estimate_min": {"type": "integer"},
                    "due_at": {"type": "string", "format": "date-time"},
                    "defer_until": {
                        "type": "string", "format": "date-time",
                        "description": "GTD start date — the task is not actionable before this.",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "queue": {"type": "string", "enum": list(QUEUES)},
                    "domains": _domains,
                    "source": {"type": "string", "default": "manual"},
                },
                "required": ["title"],
            },
            handler=tasks_add_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="tasks_update",
            description=(
                "Update one task by uid — title, priority, status, project, "
                "queue (week/focus/null), domains, parent (sub-loop of), "
                "context, energy, dates or tags. Every change records a "
                "task_events row (field-level). Completing a task with open "
                "sub-tasks is refused. Re-renders Task Backlog.md."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "e.g. TASK-0042"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string", "enum": list(TASK_STATUSES)},
                    "priority": _priority,
                    "project": {"type": "string"},
                    "context": _context,
                    "energy": _energy,
                    "estimate_min": {"type": "integer"},
                    "due_at": {"type": "string", "format": "date-time"},
                    "defer_until": {"type": "string", "format": "date-time"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "queue": _queue,
                    "domains": _domains,
                    "parent": _parent,
                    "note": {"type": "string", "description": "Recorded on the status event."},
                },
                "required": ["uid"],
            },
            handler=tasks_update_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_complete",
            description=(
                "Mark a task done by uid, stamping completed_at and recording "
                "the transition. Re-renders Task Backlog.md."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["uid"],
            },
            handler=tasks_complete_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_review",
            description=(
                "Find open tasks that may not be next actions: containers "
                "naming an area rather than a move, lines holding several "
                "actions, and bare noun phrases. READ-ONLY and advisory — "
                "these are candidates for a human to rule on, never defects "
                "to fix automatically. A meaningful share are false "
                "positives, so quote the ratio you actually found rather "
                "than the flagged count on its own. Also reports whether "
                "Task Backlog.md has been hand-edited since it was rendered."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "flag": {"type": "string", "enum": list(FLAGS)},
                },
            },
            handler=tasks_review_handler,
            annotations=ToolAnnotations(read_only_hint=True),
        ).build(),
        CustomTool(
            name="tasks_block",
            description=(
                "Record, clear or inspect blocking relationships between "
                "tasks. Only an OPEN blocker blocks — completing a blocker "
                "clears its dependents with no further action. Cycles are "
                "refused. `most_blocking` ranks blockers by how much they "
                "hold up and marks the ones whose priority is lower than the "
                "work waiting on them, which usually means the priority is "
                "wrong rather than the link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "remove"],
                        "default": "list",
                    },
                    "uid": {"type": "string", "description": "The blocked task."},
                    "blocked_by": {
                        "type": "string",
                        "description": "The task it waits on.",
                    },
                },
            },
            handler=tasks_block_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_split",
            description=(
                "Replace one compound task with several single actions. The "
                "original keeps its uid and becomes the first part; the rest are "
                "new tasks inheriting its project, priority, context, energy, "
                "tags, queue, domains and parent. Use it on a line flagged "
                "'several actions'. Re-renders Task Backlog.md."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "parts": {"type": "array", "items": {"type": "string"}, "minItems": 2,
                              "description": "The single actions, in order. The first replaces the original's title."},
                },
                "required": ["uid", "parts"],
            },
            handler=tasks_split_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="tasks_bulk_update",
            description=(
                "Apply updates to many tasks in one call — the motion a backlog "
                "sweep needs (re-tag, re-prioritise, archive, move project). "
                "Each entry takes a uid plus any fields tasks_update accepts. "
                "Renders once at the end. Reports per-uid failures rather than "
                "rolling everything back."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "uid": {"type": "string"},
                                "title": {"type": "string"},
                                "status": {"type": "string", "enum": list(TASK_STATUSES)},
                                "priority": _priority,
                                "project": {"type": "string"},
                                "context": _context,
                                "energy": _energy,
                                "due_at": {"type": "string", "format": "date-time"},
                                "defer_until": {"type": "string", "format": "date-time"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "queue": _queue,
                                "domains": _domains,
                                "parent": _parent,
                                "note": {"type": "string"},
                            },
                            "required": ["uid"],
                        },
                    },
                },
                "required": ["updates"],
            },
            handler=tasks_bulk_update_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        *loops.loops_tools(),
        *dupes.dupes_tools(),
    ]
