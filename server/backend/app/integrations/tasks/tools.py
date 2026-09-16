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
  - tasks_note_add / tasks_notes:
                       dated, append-only notes on a task (the `task_comments`
                       table). `description` stays the standing summary that is
                       embedded for duplicate detection; notes are the running
                       record — what happened, what was decided, on which day.
  - tasks_block:       record / clear / inspect what waits on what
  - tasks_structure / tasks_program / tasks_project / tasks_history:
                       the loops layer (domains → programs → projects → tasks,
                       definitions of done, field-level history) — see loops.py
  - tasks_duplicates / tasks_merge:
                       pairs that look like the same task, and folding one into
                       the other — see dupes.py
  - tasks_transfer / tasks_accept / tasks_decline:
                       multi-user hand-off, request-then-accept ("TCP not
                       UDP") — pending_owner_id moves to owner_id only when
                       the pending owner accepts.
  - tasks_nudge:       a ledger-only "status update?" ping on someone else's task.
  - tasks_users:       the household's active users, for an owner picker.
  - tasks_whoami:      the calling user's own id/name/display_name — lets a
                       client gate owner-only UI (Accept/Decline, "hand to
                       me") without hardcoding who is asking.
  - routines_add / routines_list / routines_update / routines_skip /
    routines_transfer / routines_accept / routines_decline:
                       recurring loops (chunk E3, 2026-09-03) — a routine is
                       a template, a round is one occurrence and IS a
                       `tasks` row (`routine_id` set). See routines.py.
                       `tasks_query`'s `routines` filter and every row's
                       `routine_id`/`routine_uid` are the seam back into this
                       module; `tasks_complete` mints an interval routine's
                       next round the instant its current one closes.

🔑 **These are reachable over REST as well as MCP**, with no extra work:
`POST /api/v1/tools/{name}` dispatches every registered tool through the same
chokepoint, so a browser client and a model client cannot drift apart. That
is the projection property the platform plan wants, arriving early.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.tasks import blocking, dupes, intake, loops, routines
from app.integrations.tasks.models import (
    CLAUDE_TAG_PREFIX, DEFAULT_TASK_SOURCE, KINDS, PREREQUISITE_SATISFIED_STATUSES,
    PRIORITIES, QUEUES, SEVERITIES, TASK_SOURCES, TASK_STATUSES, Routine, Task,
    TaskComment, TaskEvent, TaskProgram, TaskProject,
)
from app.models.users import User
from app.integrations.tasks.render import BACKLOG_NOTE_PATH, write_backlog_note
from app.integrations.tasks.review import FLAGS, flags_for, summarise
from app.services.text import escape_ilike
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import iso_or_none, serialize

logger = logging.getLogger(__name__)

_ROW_FIELDS = [
    "uid", "title", "status", "priority", "kind", "severity", "context", "energy",
    "estimate_min", "source", "description",
]

OPEN_STATUSES = ("inbox", "next", "waiting", "scheduled")


def _row(
    task: Task, project: TaskProject | None = None, *,
    program: TaskProgram | None = None, domains: list[str] | None = None,
    nudges: tuple[int, str | None] | None = None,
    routine_uid: str | None = None,
    pending_from_id: int | None = None,
    prerequisite: dict | None = None,
) -> dict:
    out = serialize(task, _ROW_FIELDS)
    out["due_at"] = iso_or_none(task.due_at)
    out["defer_until"] = iso_or_none(task.defer_until)
    out["completed_at"] = iso_or_none(task.completed_at)
    out["created_at"] = iso_or_none(task.created_at)
    # lios#224: null means unconfirmed (an LLM suggestion awaiting a human
    # tasks_confirm) — surfaced on the row so a client can render its own
    # "Suggested" affordance without a second call.
    out["confirmed_at"] = iso_or_none(task.confirmed_at)
    # Bumped by every column change, so it is "last edited" in the broad sense
    # — a queue move counts. Rendered on the row so an untouched line looks
    # untouched.
    out["updated_at"] = iso_or_none(task.updated_at)
    out["tags"] = list(task.tags or [])
    out["project"] = project.title if project else None
    out["program"] = program.title if program else None
    out["queue"] = task.queue
    out["queue_set_at"] = iso_or_none(task.queue_set_at)
    out["domains"] = domains or []
    # Set only on a ROUND (see routines.py). NULL for every ordinary task.
    out["routine_id"] = task.routine_id
    out["routine_uid"] = routine_uid
    # Multi-user loops: who has it, who has been asked, and whether anyone has
    # nudged for a status update. Transfer is a request, not a write — see
    # tasks_transfer/tasks_accept/tasks_decline.
    out["owner_id"] = task.owner_id
    out["pending_owner_id"] = task.pending_owner_id
    out["pending_since"] = iso_or_none(task.transfer_requested_at)
    # Who asked. Sam's first feedback (2026-09-06): a hand-over must not be
    # blind — and the receiver had been shown the *owner*, which is null for
    # an unowned loop and is not necessarily the person who handed it over.
    # Derived from the transfer-request event, not a new column; NULL unless a
    # transfer is pending.
    out["pending_from_id"] = pending_from_id if task.pending_owner_id is not None else None
    # C2 "runs with prerequisites" (lios#156). The raw id is fine here — this
    # is the tool payload, not the panel path (which never sees `prerequisite`
    # without going through the BFF's own computed field again). `null` means
    # no prerequisite.
    out["requires_task_id"] = task.requires_task_id
    out["prerequisite"] = prerequisite
    n_count, n_last = nudges or (0, None)
    out["nudges"] = n_count
    out["last_nudged_at"] = n_last
    return out


def _rows(session: Session, tasks: list[Task]) -> list[dict]:
    """Rows with their project, program and domain tags — three queries for
    the whole list rather than three per task. A task's domains are its own
    tags plus those inherited from its project and program, deduplicated."""
    projects = {p.id: p for p in session.query(TaskProject).all()}
    programs = {p.id: p for p in session.query(TaskProgram).all()}
    routine_uids = {r.id: r.uid for r in session.query(Routine).all()}
    dnames = loops.domain_names(session)
    task_ids = [t.id for t in tasks]
    nudge_rows = (
        session.query(TaskEvent.task_id, func.count(TaskEvent.id), func.max(TaskEvent.at))
        .filter(TaskEvent.field == "nudge", TaskEvent.task_id.in_(task_ids or [-1]))
        .group_by(TaskEvent.task_id)
        .all()
    )
    nudges = {tid: (count, iso_or_none(last)) for tid, count, last in nudge_rows}
    requesters = _requesters_of_pending_transfers(
        session, [t.id for t in tasks if t.pending_owner_id is not None],
    )
    # C2 "runs with prerequisites" (lios#156): one query for the whole list,
    # same shape as projects/programs/routine_uids above.
    req_ids = [t.requires_task_id for t in tasks if t.requires_task_id]
    prereq_tasks = (
        {r.id: r for r in session.query(Task).filter(Task.id.in_(req_ids)).all()}
        if req_ids else {}
    )
    out = []
    for t in tasks:
        project = projects.get(t.project_id) if t.project_id else None
        program = programs.get(project.program_id) if project and project.program_id else None
        seen: list[str] = []
        for key in (("task", t.id), ("project", project.id if project else -1), ("program", program.id if program else -1)):
            for name in dnames.get(key, []):
                if name not in seen:
                    seen.append(name)
        prereq_task = prereq_tasks.get(t.requires_task_id) if t.requires_task_id else None
        prerequisite = (
            {
                "text": prereq_task.title,
                "satisfied": prereq_task.status in PREREQUISITE_SATISFIED_STATUSES,
            }
            if prereq_task is not None else None
        )
        out.append(_row(
            t, project, program=program, domains=seen, nudges=nudges.get(t.id),
            routine_uid=routine_uids.get(t.routine_id) if t.routine_id else None,
            pending_from_id=requesters.get(t.id),
            prerequisite=prerequisite,
        ))
    return out


def _render(session: Session) -> None:
    """Re-render the vault view. The render is unconditional — see
    `render.py`'s module docstring for why (lios, 2026-09-14) — so the only
    way this can still fail is a real fault in the write itself (an empty
    ledger over a populated file, a short/failed disk write), never a
    refused overwrite of a hand edit.

    Never re-raises: every caller sits after its own `session.commit()`, so
    letting a `write_backlog_note` failure propagate as a top-level
    `{"error": ...}` would misreport an already-committed write as failed
    (lios#202 — nine `tasks_add` calls each reported failure while the row
    had actually been created, inviting a retry that would duplicate it).
    Logged and swallowed instead; the ledger write already stands regardless
    of whether its markdown mirror kept up.

    Request-scoped: `write_backlog_note` resolves the vault from the bound
    user, so this only covers the per-request path. A scheduler tick has no
    bound user — it must call `render_all_vaults` instead, which already
    catches `RuntimeError` per-user rather than propagating it."""
    try:
        write_backlog_note(session)
    except RuntimeError as exc:
        logger.warning("Task Backlog.md not re-rendered: %s", exc)


def _render_other(session: Session, user_id: int | None) -> None:
    """Best-effort re-render of the OTHER party's vault after a hand-over.

    `_render` only refreshes the caller's file, so after a transfer, accept or
    decline the other person's `Task Backlog.md` was stale until the
    15-minute tick. Same guards as `render_all_vaults`: a user with no vault
    on disk is skipped, and a refusal (`drifted`) or any other failure is
    logged, never raised — the write itself has already committed and must
    not be reported as failed because someone else's file could not be."""
    if user_id is None or user_id == current_user_id():
        return
    from app.services import vault_paths

    try:
        note = vault_paths.resolve(BACKLOG_NOTE_PATH, user_id_override=user_id)
        if not note.parent.is_dir():
            return
        write_backlog_note(session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - advisory; the caller's write stands
        logger.warning("Task Backlog.md not re-rendered for user %s: %s", user_id, exc)


def render_all_vaults(session: Session) -> list[str]:
    """The scheduler-side render. There is no request and so no bound user;
    `current_user_id()` raises, and `write_backlog_note(session)` with it —
    which is how the first `reminders_inlet_tick` after its deploy
    (2026-09-04 10:33) captured ten reminders and then died in the render,
    leaving the ledger and the file disagreeing until someone forced one.

    So bind each user explicitly: render into every active user's vault that
    exists on disk (a user with no vault — the agent — is skipped, same rule
    the now-deleted `apple_reminders.backlog_sync` used to follow). One vault
    refusing (`drifted`) must not sink the whole tick or the other vaults: it
    is logged and the next tick tries again. Returns the names rendered for.
    """
    from app.models.users import User
    from app.services import vault_paths

    rendered: list[str] = []
    users = session.query(User).filter_by(is_active=True).order_by(User.id).all()
    for user in users:
        note = vault_paths.resolve(BACKLOG_NOTE_PATH, user_id_override=user.id)
        if not note.parent.is_dir():
            continue
        try:
            write_backlog_note(session, user_id=user.id)
        except RuntimeError as exc:
            logger.warning("backlog render skipped for %s: %s", user.name, exc)
            continue
        rendered.append(user.name)
    return rendered


def _get(session: Session, uid: str) -> Task:
    task = session.query(Task).filter(Task.uid == uid).one_or_none()
    if task is None:
        raise ValueError(f"Unknown task uid: {uid}")
    return task


def _resolve_user(session: Session, value: str) -> int:
    """'me' -> the caller; else a user id or a `users.name`. Raises with the
    list of real users on a miss — the same shape as the unknown-project
    error, because a caller assigning a task to a name it half-remembers is
    a UX bug, not a 500."""
    if value == "me":
        return current_user_id()
    if str(value).isdigit():
        return int(value)
    user = session.query(User).filter(User.name == value).one_or_none()
    if user is None:
        names = sorted(u.name for u in session.query(User).all())
        raise ValueError(f"Unknown user: {value}. Existing users: {', '.join(names)}")
    return user.id


def _resolve_requires_task(session: Session, uid: str, for_task: Task | None = None) -> int:
    """Resolve a C2 prerequisite reference (lios#156) by uid.

    Raises the same "Unknown task uid" error whether the uid does not exist
    at all or exists but is not visible to the caller (owned by someone
    else) — a prerequisite must not leak a task the caller cannot see, and a
    distinguishable error message would do exactly that by confirming the
    uid is real. Visibility mirrors `_scope_owner`'s default: unowned or
    owned by the caller; a task owned by a different household member is
    not a valid prerequisite reference for this caller.
    """
    ref = session.query(Task).filter(Task.uid == uid).one_or_none()
    if ref is None or not (ref.owner_id is None or ref.owner_id == current_user_id()):
        raise ValueError(f"Unknown task uid: {uid}")
    if for_task is not None and ref.id == for_task.id:
        raise ValueError(f"{for_task.uid} cannot require itself.")
    return ref.id


HOUSEHOLD = "household"


def _scope_owner(session: Session, args: dict) -> int | None:
    """The owner a read tool scopes to. Absent -> the CALLER; `"household"`
    -> None (no owner filter, everyone's); else `_resolve_user`.

    The ledger is household-shared by design (assignment is a field on the
    row, not row ownership), and until 2026-09-06 every read tool returned
    everyone's rows unless told otherwise. That was invisible while one
    person used it. The first `/tunetasks` run from Sam's Mac pulled 201
    tasks, 142 of them Alex's, because the command's own template — like
    every template — never passes `owner`. Defaulting the SERVER to the
    caller fixes every client at once (the loops app had already patched
    the same default into itself, PR #104); widening is now the explicit
    ask, the same shape `tasks_structure` took with `scope="household"`.
    """
    value = args.get("owner")
    if value is None or value == "":
        return current_user_id()
    if value == HOUSEHOLD:
        return None
    return _resolve_user(session, value)


def _blocked_on_me_uids(session: Session) -> set[str]:
    """Open tasks I own that are the blocker holding up someone ELSE's open
    task — the priority-inversion-adjacent question 'what am I sitting on'."""
    me = current_user_id()
    owners = dict(session.query(Task.uid, Task.owner_id).all())
    out: set[str] = set()
    for blocked_uid, blocker_uids in blocking.open_blockers(session).items():
        if owners.get(blocked_uid) == me:
            continue  # I'm the one waiting; not the interesting direction here.
        for blocker_uid in blocker_uids:
            if owners.get(blocker_uid) == me:
                out.add(blocker_uid)
    return out


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
        task_id=task.id, routine_id=task.routine_id,
        from_status=task.status, to_status=new_status,
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

    # lios#224: an unconfirmed (LLM-suggested, not yet reviewed) task is
    # excluded from every "active" read by default — the code gate the
    # confirmation column exists for. `include_unconfirmed=True` includes
    # them alongside confirmed rows; `unconfirmed_only=True` is the
    # dedicated "just the suggestions" read the rendered backlog's
    # `## Suggested` section and the Loops UI's Suggested strip use.
    if args.get("unconfirmed_only"):
        q = q.filter(Task.confirmed_at.is_(None))
    elif not args.get("include_unconfirmed"):
        q = q.filter(Task.confirmed_at.isnot(None))

    # `statuses` is the general filter — any subset of the status vocabulary,
    # e.g. ("someday",) or ("someday", "dropped"). `status` (singular) is the
    # older one-value form, kept for existing callers. `include_done` is now a
    # deprecated alias for OPEN_STATUSES + ("done",) — that is what every
    # caller of it actually meant; it used to mean "remove the status filter
    # entirely", which silently let `someday` and `dropped` rows through as
    # if they were open (the root cause of a row moved to Someday reappearing
    # in the open list on reconcile). Precedence: statuses > status >
    # include_done > default-open.
    statuses = args.get("statuses")
    status = args.get("status")
    if statuses:
        invalid = sorted(set(statuses) - set(TASK_STATUSES))
        if invalid:
            raise ValueError(f"invalid status in statuses: {invalid}")
        q = q.filter(Task.status.in_(statuses))
    elif status:
        q = q.filter(Task.status == status)
    elif args.get("include_done"):
        q = q.filter(Task.status.in_(OPEN_STATUSES + ("done",)))
    else:
        q = q.filter(Task.status.in_(OPEN_STATUSES))

    if args.get("priority"):
        q = q.filter(Task.priority == args["priority"])
    if args.get("kind"):
        _check_kind(args["kind"])
        q = q.filter(Task.kind == args["kind"])
    if args.get("severity"):
        _check_severity(args["severity"])
        q = q.filter(Task.severity == args["severity"])
    if args.get("context"):
        q = q.filter(Task.context == args["context"])
    if args.get("energy"):
        q = q.filter(Task.energy == args["energy"])
    if args.get("tag"):
        q = q.filter(Task.tags.any(args["tag"]))
    if args.get("tag_prefix"):
        # Any tag under a namespace — `#claude/` matches all three roles. The
        # array has no prefix operator, so join it with a separator no tag can
        # contain and test "separator + prefix" appears: that anchors the match
        # to the START of an element, so '#ho' matches '#home' (a prefix, as
        # asked) but 'ome' matches nothing. No unnest, so nothing to correlate.
        from sqlalchemy import literal
        sep = "\x1f"
        joined = literal(sep).op("||")(func.array_to_string(Task.tags, sep))
        q = q.filter(joined.like("%" + sep + escape_ilike(args["tag_prefix"]) + "%"))
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
    if args.get("completed_since"):
        # A client wanting "today's done rows" alongside an open lens (the
        # loops app's collapsed "closed today" group) needs this as its own
        # filter — asking for every historically-done row and paging through
        # it competes with open rows for the same limit, which is how done
        # rows used to crowd open ones out of a capped result.
        q = q.filter(Task.completed_at.isnot(None), Task.completed_at >= args["completed_since"])
    # The three multi-user lenses are about the caller BY CONSTRUCTION and
    # the rows they return are owned by someone else (a transfer waiting on
    # me, work of mine blocking theirs, a loop I handed over), so the default
    # owner scope would empty them. They take no owner filter unless one is
    # asked for by name.
    by_construction = args.get("pending_for_me") or args.get("blocked_on_me") or args.get("handed_by_me")
    owner_id = None if (by_construction and not args.get("owner")) else _scope_owner(session, args)
    if owner_id is not None:
        q = q.filter(Task.owner_id == owner_id)
    if args.get("pending_for_me"):
        q = q.filter(Task.pending_owner_id == current_user_id())
    if args.get("blocked_on_me"):
        q = q.filter(Task.uid.in_(_blocked_on_me_uids(session) or {"\x00"}))
    if args.get("handed_by_me"):
        # Loops I handed to someone else and still want to watch (Sam's
        # first feedback, 2026-09-06: a hand-over must not be a blind
        # transfer). Derived from the transfer events: the request event's
        # actor is the person handing over (old_value is the previous
        # *pending* owner, usually none). A loop stays in this view for as
        # long as it is someone else's, and leaves it if it is handed back.
        me = current_user_id()
        handed_ids = (
            session.query(TaskEvent.task_id)
            .filter(
                TaskEvent.field == "transfer", TaskEvent.actor_id == me,
                TaskEvent.note.like("requested%"),
            )
        )
        q = q.filter(Task.id.in_(handed_ids), Task.owner_id != me)
    if args.get("unfiled"):
        q = q.filter(Task.project_id.is_(None))
    routines_filter = args.get("routines", "all")
    if routines_filter == "only":
        q = q.filter(Task.routine_id.isnot(None))
    elif routines_filter == "exclude":
        q = q.filter(Task.routine_id.is_(None))
    elif routines_filter != "all":
        raise ValueError("routines must be one of 'only', 'exclude', 'all'")

    limit = min(int(args.get("limit", 50)), 500)
    tasks = q.order_by(Task.sort_order, Task.id).limit(limit).all()

    return json.dumps({"count": len(tasks), "tasks": _rows(session, tasks)})


def _unowned_open(session: Session) -> dict:
    """Open tasks with no owner — the hygiene gap `tasks_add` used to leave
    open (owner_id null until someone claimed or was handed the task), and
    the ask a client renders owner chips can't answer for a row with none.
    Kept small on purpose: a count plus the list, nothing scored or judged.
    Rounds are included too — a routine's `default_owner_id` should always
    be set, so a null one here is exactly as real a gap as an ordinary task's."""
    rows = (
        session.query(Task)
        .filter(
            Task.status.in_(OPEN_STATUSES), Task.owner_id.is_(None),
            Task.confirmed_at.isnot(None),
        )
        .order_by(Task.sort_order, Task.id)
        .all()
    )
    return {
        "count": len(rows),
        "tasks": [{"uid": t.uid, "title": t.title} for t in rows],
    }


def tasks_review_handler(session: Session, args: dict) -> str:
    """Read-only. Judges nothing — it hands a person candidates to rule on.

    A round (`routine_id` set) is excluded: its title is the routine's own
    title, chosen once when the routine was created, not written fresh each
    cycle — flagging it every 28 days is noise a human has already ruled on.
    (`unowned_open` below is a separate check and does NOT exclude rounds —
    see its own docstring.)
    """
    q = session.query(Task).filter(
        Task.status.in_(OPEN_STATUSES), Task.routine_id.is_(None),
        # lios#224: an unconfirmed suggestion is not yet a real line to rule
        # on — see tasks_query's same filter.
        Task.confirmed_at.isnot(None),
    )
    if args.get("project"):
        ids = [
            p.id for p in session.query(TaskProject)
            .filter(TaskProject.title.ilike(f"%{escape_ilike(args['project'])}%")).all()
        ]
        q = q.filter(Task.project_id.in_(ids or [-1]))
    # Same argument `tasks_query` takes, same default: the caller's own
    # lines (Loops, 2026-09-06 — the summary strip's `needs_rewriting`
    # counted Alex's lines for Sam; then `/tunetasks` did the same from her
    # Mac). `owner="household"` flags everyone's. `unowned_open` below is
    # NEVER scoped: an unowned loop is nobody's, and is the one hygiene gap
    # that must show to whoever looks.
    owner_id = _scope_owner(session, args)
    if owner_id is not None:
        q = q.filter(Task.owner_id == owner_id)
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
    # A hygiene problem a client's Problems section wants on every load, not
    # a bespoke tool and round trip of its own. (The rendered file's own
    # drift state used to be reported alongside this — removed 2026-09-14
    # with the render's edit-detection guard; the file is a pure write-only
    # projection now, so there is nothing to report.)
    out["unowned_open"] = _unowned_open(session)
    return json.dumps(out)


def tasks_block_handler(session: Session, args: dict) -> str:
    action = args.get("action", "list")
    if action == "add":
        blocking.add_blocker(session, args["uid"], args["blocked_by"])
    elif action == "remove":
        blocking.remove_blocker(session, args["uid"], args["blocked_by"])
    elif action != "list":
        raise ValueError(f"Unknown action: {action}")

    # `owner` scopes the BLOCKED side to that person's loops: their blocked
    # rows, what is holding those up, ranked. Defaults to the caller;
    # `owner="household"` is the whole graph — which a client attaching
    # blockers to rows it already scoped (or to the caller-by-construction
    # `blocked_on_me` rows, owned by someone else) still wants.
    owner_id = _scope_owner(session, args)

    # Returned for every action, so a write's answer shows its effect on the
    # graph rather than just confirming itself.
    return json.dumps({
        "blocked": blocking.open_blockers(session, owner_id=owner_id),
        "most_blocking": blocking.most_blocking(session, owner_id=owner_id),
        # blocker uid -> the open tasks it's holding up (uid+title),
        # unlimited — what the "blocked_on_me" lens attaches per row.
        "blocking_of": blocking.blocking_of(session, owner_id=owner_id),
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
    #
    # owner_id defaults to the caller — a create path with no owner argument
    # is not a design gap to route around: a hand-over is `tasks_transfer`
    # (request/accept, "TCP not UDP"), so `tasks_add` deliberately has no
    # `owner` argument that could set someone else's task without their
    # accept. See the module docstring / PR notes for the decision.
    kind = args.get("kind") or "task"
    _check_kind(kind)
    _check_severity(args.get("severity"))
    source = args.get("source") or DEFAULT_TASK_SOURCE
    _check_source(source)
    # lios#224: the confirmation gate. Human-initiated callers keep the
    # default (True) and the task is active immediately, same as before this
    # change. An extraction/suggestion path (today: `/harvest`) passes
    # `confirmed=False` and the row is excluded from every "active" read
    # until a human calls `tasks_confirm` — see that tool and `tasks_query`'s
    # `include_unconfirmed`.
    confirmed = args.get("confirmed", True)
    now = datetime.now(timezone.utc)
    task = Task(
        uid=_next_uid(session),
        title=args["title"][:300],
        description=args.get("description"),
        status=args.get("status", "next"),
        priority=args.get("priority"),
        kind=kind,
        severity=args.get("severity"),
        project_id=project.id if project else None,
        context=args.get("context"),
        energy=args.get("energy"),
        estimate_min=args.get("estimate_min"),
        due_at=args.get("due_at"),
        defer_until=args.get("defer_until"),
        tags=args.get("tags") or [],
        source=source,
        confirmed_at=now if confirmed else None,
        owner_id=current_user_id(),
        created_at=now,
        sort_order=(session.query(Task).count() + 1) * 1000,
    )
    session.add(task)
    session.flush()
    session.add(TaskEvent(
        task_id=task.id, from_status=None, to_status=task.status,
        actor_id=current_user_id(), note="created",
    ))
    # Recorded the same way any other owner_id set is (see _field_event) —
    # so "who owned this from the start" shows up in tasks_history exactly
    # like a later reassignment would, not as a silent side effect of create.
    _field_event(session, task, "owner_id", None, task.owner_id)
    if args.get("requires_task"):
        task.requires_task_id = _resolve_requires_task(session, args["requires_task"], task)
        _field_event(session, task, "requires_task_id", None, task.requires_task_id)
    if args.get("queue"):
        _apply_update(session, task, {"queue": args["queue"]})
    if args.get("domains"):
        loops.set_domain_tags(session, args["domains"], task=task)
    dupes.enqueue(session, task)
    session.commit()
    _render(session)
    return json.dumps({"created": _rows(session, [task])[0]})


_UPDATABLE = (
    "title", "description", "priority", "kind", "severity", "context", "energy",
    "estimate_min", "due_at", "defer_until", "tags",
)


def _check_kind(kind) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")


def _check_severity(severity) -> None:
    if severity not in (None, *SEVERITIES):
        raise ValueError(f"severity must be one of {SEVERITIES} or null")


def _check_source(source: str) -> None:
    """lios#224: validated at every write site, never a DB CHECK — see
    `TASK_SOURCES`'s docstring in models.py for why."""
    if source not in TASK_SOURCES:
        raise ValueError(f"source must be one of {TASK_SOURCES}")


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
    # Checked before anything is written so a bad value refuses the whole
    # update rather than tripping the CHECK constraint at commit — which in
    # `tasks_bulk_update` would take the other 39 good changes down with it.
    if "kind" in args:
        _check_kind(args["kind"])
    if "severity" in args:
        _check_severity(args["severity"])
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
    if "requires_task" in args:
        ref = args["requires_task"]
        new_id = None if ref is None else _resolve_requires_task(session, ref, task)
        _field_event(session, task, "requires_task_id", task.requires_task_id, new_id)
        task.requires_task_id = new_id
    if "status" in args:
        _set_status(session, task, args["status"], note=args.get("note"))
    # Every path that changes text or status keeps the vector in step; a
    # closed task's vector is removed (see dupes.py).
    dupes.enqueue(session, task)


def _note_row(note: TaskComment, names: dict[int, str]) -> dict:
    return {
        "id": note.id,
        "body": note.body,
        "at": iso_or_none(note.created_at),
        "author": names.get(note.author_id) if note.author_id else None,
    }


def _notes_for(session: Session, task: Task) -> list[dict]:
    notes = (
        session.query(TaskComment)
        .filter(TaskComment.task_id == task.id)
        .order_by(TaskComment.created_at, TaskComment.id)
        .all()
    )
    ids = {n.author_id for n in notes if n.author_id}
    names = {u.id: u.display_name for u in session.query(User).filter(User.id.in_(ids or {-1}))} if ids else {}
    return [_note_row(n, names) for n in notes]


def tasks_notes_handler(session: Session, args: dict) -> str:
    task = _get(session, args["uid"])
    notes = _notes_for(session, task)
    return json.dumps({"uid": task.uid, "count": len(notes), "notes": notes})


def tasks_note_add_handler(session: Session, args: dict) -> str:
    """Append one dated note. Never edits or deletes — the table is the
    running record, and a record you can rewrite is not one. The standing
    summary is still `description` (tasks_update); this is what happened on a
    day. A `task_events` row marks the addition so the history view shows it
    beside status changes."""
    task = _get(session, args["uid"])
    body = (args.get("body") or "").strip()
    if not body:
        raise ValueError("body is empty")
    note = TaskComment(task_id=task.id, author_id=current_user_id(), body=body)
    session.add(note)
    session.add(TaskEvent(
        task_id=task.id, from_status=None, to_status=task.status,
        actor_id=current_user_id(), field="note", old_value=None,
        new_value=body[:200],
    ))
    # Touch the row so `updated_at` says a note landed.
    task.updated_at = datetime.now(timezone.utc)
    session.commit()
    names = {u.id: u.display_name for u in session.query(User).filter(User.id == (note.author_id or -1))}
    return json.dumps({"added": _note_row(note, names), "count": session.query(TaskComment).filter(TaskComment.task_id == task.id).count()})


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
    # Interval routines mint their next round the instant this one closes;
    # fixed/window routines wait for the scheduler tick (see routines.py).
    minted = routines.mint_next_on_complete(session, task)
    session.commit()
    _render(session)
    out = {"completed": _rows(session, [task])[0]}
    if minted is not None:
        out["next_round"] = minted.uid
    return json.dumps(out)


def _transfer_event(
    session: Session, task: Task, note: str, *, old: int | None = None, new: int | None = None,
) -> None:
    """One `task_events` row per transfer-lifecycle step (requested / accepted
    / declined). `field="transfer"`; `old_value`/`new_value` hold the relevant
    user ids as text so `tasks_history` can show who handed a task to whom
    without a bespoke event shape."""
    session.add(TaskEvent(
        task_id=task.id, from_status=None, to_status=task.status,
        actor_id=current_user_id(), field="transfer",
        old_value=str(old) if old is not None else None,
        new_value=str(new) if new is not None else None,
        note=note,
    ))


def tasks_transfer_handler(session: Session, args: dict) -> str:
    """Transfer is a REQUEST, not a write ("TCP not UDP"). This only sets
    `pending_owner_id` — `owner_id` does not move until the pending owner
    calls `tasks_accept`. Calling this again before it is answered redirects
    or cancels the outstanding request (the previous target simply never
    sees it land)."""
    task = _get(session, args["uid"])
    to_id = _resolve_user(session, args["to_user"])
    old_pending = task.pending_owner_id
    task.pending_owner_id = to_id
    task.transfer_requested_at = datetime.now(timezone.utc)
    _transfer_event(
        session, task,
        "requested" if old_pending is None else "requested (redirected)",
        old=old_pending, new=to_id,
    )
    session.commit()
    _render(session)
    _render_other(session, to_id)
    if to_id != current_user_id():
        requester = _display_name(session, current_user_id())
        _notify(f"lios: {requester} handed you a task", f"{task.uid}: {task.title}", to_id)
    return json.dumps({"transferred": _rows(session, [task])[0]})


def tasks_accept_handler(session: Session, args: dict) -> str:
    """Only the pending owner may accept. Moves pending -> owner_id."""
    task = _get(session, args["uid"])
    me = current_user_id()
    if task.pending_owner_id != me:
        raise ValueError(
            f"{task.uid} has no transfer pending for you to accept."
        )
    old_owner = task.owner_id
    requester_id = _requester_of_pending_transfer(session, task)
    task.owner_id = me
    task.pending_owner_id = None
    task.transfer_requested_at = None
    _transfer_event(session, task, "accepted", old=old_owner, new=me)
    session.commit()
    _render(session)
    _render_other(session, requester_id)
    if requester_id is not None and requester_id != me:
        acceptor = _display_name(session, me)
        _notify(f"lios: {acceptor} accepted a task", f"{task.uid}: {task.title}", requester_id)
    return json.dumps({"accepted": _rows(session, [task])[0]})


def tasks_decline_handler(session: Session, args: dict) -> str:
    """Only the pending owner may decline a transfer. Clears the pending
    state without ever moving owner_id; an optional note is recorded as a
    dated comment so the requester sees why, not just that it happened.

    lios#224: **also the dismiss path for an unconfirmed (LLM-suggested)
    task** — a suggestion has no pending transfer to decline, but a person
    still needs a way to say "no, not this one" without it lingering
    unconfirmed forever. A task with `pending_owner_id` set always takes the
    transfer-decline branch (that check comes first); a task with neither a
    pending transfer nor a confirmation is dropped instead, with the same
    optional-note-as-comment shape.
    """
    task = _get(session, args["uid"])
    me = current_user_id()
    if task.pending_owner_id is None and task.confirmed_at is None:
        note = (args.get("note") or "").strip()
        _set_status(session, task, "dropped", note=note or "declined suggestion")
        if note:
            session.add(TaskComment(task_id=task.id, author_id=me, body=note))
        session.commit()
        _render(session)
        return json.dumps({"declined": _rows(session, [task])[0]})
    if task.pending_owner_id != me:
        raise ValueError(
            f"{task.uid} has no transfer pending for you to decline."
        )
    pending = task.pending_owner_id
    requester_id = _requester_of_pending_transfer(session, task)
    task.pending_owner_id = None
    task.transfer_requested_at = None
    note = (args.get("note") or "").strip()
    _transfer_event(
        session, task, "declined" + (f": {note}" if note else ""),
        old=pending, new=None,
    )
    if note:
        session.add(TaskComment(task_id=task.id, author_id=me, body=note))
    session.commit()
    _render(session)
    _render_other(session, requester_id)
    if requester_id is not None and requester_id != me:
        decliner = _display_name(session, me)
        body = f"{task.uid}: {task.title}" + (f" — {note}" if note else "")
        _notify(f"lios: {decliner} declined a task", body, requester_id)
    return json.dumps({"declined": _rows(session, [task])[0]})


def tasks_confirm_handler(session: Session, args: dict) -> str:
    """lios#224: the human-accept half of the confirmation gate. Sets
    `confirmed_at` on an unconfirmed (LLM-suggested) task so it starts
    showing up in every "active" read — `tasks_query`'s default,
    `tasks_review`, the rendered backlog's main sections, the Loops app's
    default lenses. A no-op (but not an error) on a task that is already
    confirmed, so a client doesn't need to check first."""
    task = _get(session, args["uid"])
    already = task.confirmed_at is not None
    if not already:
        task.confirmed_at = datetime.now(timezone.utc)
        _field_event(session, task, "confirmed_at", None, task.confirmed_at)
    session.commit()
    _render(session)
    return json.dumps(
        {"confirmed": _rows(session, [task])[0], "already_confirmed": already}
    )


def tasks_nudge_handler(session: Session, args: dict) -> str:
    """A `task_events` row (field='nudge') plus a 'status update?' comment,
    every time — the ledger always records that a nudge was asked for. The
    PUSH to the task's owner is rate-limited separately (`_NUDGE_PUSH_COOLDOWN`):
    a repeat nudge within the window still lands in the ledger but does not
    ring the phone again, since it would be a repeat of the exact same
    question. See apps/loops' inbox lens and the row's `nudges`/
    `last_nudged_at` for how the ledger side surfaces."""
    task = _get(session, args["uid"])
    note = (args.get("note") or "").strip()

    last_nudge_at = (
        session.query(func.max(TaskEvent.at))
        .filter(TaskEvent.task_id == task.id, TaskEvent.field == "nudge")
        .scalar()
    )
    now = datetime.now(timezone.utc)
    should_push = last_nudge_at is None or (now - last_nudge_at) >= _NUDGE_PUSH_COOLDOWN

    body = "status update?" + (f" {note}" if note else "")
    session.add(TaskComment(task_id=task.id, author_id=current_user_id(), body=body))
    session.add(TaskEvent(
        task_id=task.id, from_status=None, to_status=task.status,
        actor_id=current_user_id(), field="nudge", note=note or None,
    ))
    session.commit()

    notified = False
    me = current_user_id()
    if should_push and task.owner_id is not None and task.owner_id != me:
        nudger = _display_name(session, me)
        push_body = f"{task.uid}: {task.title}" + (f" — {note}" if note else "")
        _notify(f"lios: {nudger} nudged you for a status update", push_body, task.owner_id)
        notified = True

    return json.dumps({"nudged": _rows(session, [task])[0], "notified": notified})


def tasks_users_handler(session: Session, args: dict) -> str:
    """The household's users — for the owner/"hand to…" picker. Small and
    read-only; no reason to route this through a bespoke endpoint per app."""
    users = session.query(User).filter(User.is_active.is_(True)).order_by(User.id).all()
    return json.dumps({"users": [
        {"id": u.id, "name": u.name, "display_name": u.display_name} for u in users
    ]})


def tasks_whoami_handler(session: Session, args: dict) -> str:
    """The caller's own identity. A client needs this to gate owner-only UI
    (Accept/Decline, "hand to me") *without* hardcoding who is asking — the
    same reason `tasks_users` reads the users table instead of a fixed list.
    Server-side enforcement (tasks_accept/tasks_decline refusing a non-owner)
    is unchanged; this only lets a client match its own display to reality."""
    me = current_user_id()
    user = session.query(User).filter(User.id == me).one_or_none()
    if user is None:
        raise ValueError(f"Unknown user: {me}")
    return json.dumps({"id": user.id, "name": user.name, "display_name": user.display_name})


def tasks_absence_alerts_handler(session: Session, args: dict) -> str:
    """Open absence-detection findings (R5, Wave 2) — a routine whose window
    closed with no round completed, a `waiting` item past due with no note,
    or a snag unanswered for weeks. Scoped like `system_alerts` axis 5: the
    caller sees their own (owner_id matches) plus every household-shared one
    (owner_id NULL, e.g. an unowned snag). See `absence.py`'s module
    docstring for why this is its own tool rather than a `system_alerts`
    axis — a real dependency-graph cycle, not a stylistic choice."""
    from app.integrations.tasks import absence

    scope = None if args.get("household") else current_user_id()
    rows = absence.open_alerts_for(session, owner_id=scope)
    return json.dumps({"count": len(rows), "alerts": rows})


# ─── notifications (best-effort; never raise into a caller's write path) ───
#
# Same stance as `notifications.facade.send()` and every other caller of it
# (household's capture confirmation, inbox's transcript push): these are
# one-off, personal acknowledgements of something that just happened, not a
# recurring infrastructure problem — so they go through the ad-hoc `send()`
# path, not `notifications.sweep`'s ledgered persistence-gate/quiet-hours
# machinery, which is built and reserved for that different question. See
# `app/integrations/household/tools.py::_notify_capture_confirmation` for the
# same call shape and the same reasoning.
#
# tasks_nudge is the one exception worth a guard: unlike a transfer or an
# accept, a nudge can legitimately be sent more than once for the same task in
# a short span (someone impatient tapping it twice), and here the push really
# would be a repeat of the exact same question. `_NUDGE_PUSH_COOLDOWN` throttles
# the *push* only — the task_events row and comment are written every time,
# same as before, so the ledger still shows every nudge asked.
_NUDGE_PUSH_COOLDOWN = timedelta(minutes=30)


def _display_name(session: Session, user_id: int | None) -> str:
    if user_id is None:
        return "someone"
    user = session.query(User).filter(User.id == user_id).one_or_none()
    return user.display_name if user else f"user {user_id}"


def _notify(title: str, body: str, user_id: int | None) -> None:
    """Best-effort push via the notify.push capability. Never raises: a
    dropped notification must never fail the write that triggered it."""
    if user_id is None:
        return
    try:
        from app.plugin.capabilities import get_capability
        get_capability("notify.push").send(title, body, "recovery", user_id=user_id, source="tasks")
    except Exception:  # noqa: BLE001 - notify.push may not be registered/configured
        logger.debug("tasks: notify.push unavailable", exc_info=True)


def _requester_of_pending_transfer(session: Session, task: Task) -> int | None:
    """Who asked for the transfer that is being accepted or declined — the
    actor of the most recent 'requested' transfer event for this task. Used
    so acceptance/decline notifies the person who is actually waiting to
    hear back, not a hardcoded household member."""
    return _requesters_of_pending_transfers(session, [task.id]).get(task.id)


def _requesters_of_pending_transfers(session: Session, task_ids: list[int]) -> dict[int, int | None]:
    """The same answer for a whole listing in one query: task id -> actor of
    its most recent 'requested' transfer event. Feeds each row's
    `pending_from_id`, so the receiving side can say who handed it over. A
    redirect ("requested (redirected)") counts — the redirecting actor is the
    one now asking. Tasks with no request event are absent from the result."""
    if not task_ids:
        return {}
    events = (
        session.query(TaskEvent.task_id, TaskEvent.actor_id)
        .filter(TaskEvent.task_id.in_(task_ids), TaskEvent.field == "transfer")
        .filter(TaskEvent.note.ilike("requested%"))
        .order_by(TaskEvent.at.desc(), TaskEvent.id.desc())
        .all()
    )
    out: dict[int, int | None] = {}
    for task_id, actor_id in events:
        out.setdefault(task_id, actor_id)  # first seen is the latest
    return out


def tasks_split_handler(session: Session, args: dict) -> str:
    """One compound line becomes several single actions.

    The original keeps its uid and takes the first part as its title, so
    links, history and its place in the file survive; the rest are new tasks
    that inherit everything that would otherwise have to be re-typed —
    project, priority, kind, severity, context, energy, tags, queue, domains,
    parent. This is
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
    # A split half inherits the original's owner like everything else it
    # inherits (project, priority, context, ...); if the original itself has
    # none — an old, pre-default row — the caller is the fallback, same rule
    # as tasks_add, rather than minting another unowned open loop.
    owner_id = task.owner_id if task.owner_id is not None else current_user_id()
    made = []
    base = session.query(Task).count()
    for i, part in enumerate(parts[1:], start=1):
        new = Task(
            uid=_next_uid(session),
            title=part[:300],
            status=task.status if task.status in OPEN_STATUSES else "next",
            priority=task.priority,
            kind=task.kind,
            severity=task.severity,
            project_id=task.project_id,
            context=task.context,
            energy=task.energy,
            tags=list(task.tags or []),
            source="split",
            # A person invoked this split, so a new half is confirmed
            # immediately, same as tasks_add's human-initiated default.
            confirmed_at=datetime.now(timezone.utc),
            owner_id=owner_id,
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
        _field_event(session, new, "owner_id", None, new.owner_id)
        if own_domains:
            loops.set_domain_tags(session, own_domains, task=new)
        if parent:
            loops.set_parent(session, new, parent)
        dupes.enqueue(session, new)
        made.append(new)

    session.commit()
    _render(session)
    return json.dumps(
        {"kept": _rows(session, [task])[0], "created": _rows(session, made)}
    )


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
    _kind = {
        "type": "string", "enum": list(KINDS),
        "description": (
            "kind: task (default) | bug | feature | chore — for household "
            "things (a broken appliance, a wanted household capability), "
            "never for lios development items, which are GitHub Issues on "
            "cograda/lios, not ledger rows; severity applies to bugs/features."
        ),
    }
    _severity = {
        "type": ["string", "null"], "enum": [*SEVERITIES, None],
        "description": "Severity of a bug or feature: critical | high | medium | low. Null clears it.",
    }
    _parent = {
        "type": ["string", "null"],
        "description": "uid of the task this is a sub-loop of (part_of); null to detach. A parent cannot complete while a child is open.",
    }

    return [
        CustomTool(
            name="tasks_query",
            description=(
                "Query the household task backlog. Open tasks by default; "
                "filter by status, priority, kind (task/bug/feature/chore — "
                "for household things; lios development items are GitHub "
                "Issues on cograda/lios, not ledger rows), "
                "severity, context (errand/sitdown), energy "
                "(quick/deep), tag, project, program, queue (week/focus), "
                "domain, free text, due date, overdue, owner (defaults to "
                "YOUR tasks; owner='household' for everyone's), or the two "
                "multi-user lenses: pending_for_me (transfers awaiting your "
                "acceptance) and blocked_on_me (open tasks you own that are "
                "the blocker holding up someone else's open task). "
                "'statuses' takes any subset of the status vocabulary (e.g. "
                "['someday', 'dropped']) and is the general way to see "
                "non-open rows — the removed status filter never means 'all "
                "statuses'. 'status' is the older single-value form. "
                "'include_done' is a deprecated alias for open+done (it does "
                "NOT include someday/dropped). "
                "'routines' filters rounds (tasks minted from a routine "
                "template, routine_id set): 'only', 'exclude', or 'all' "
                "(default). Every row carries routine_id/routine_uid, null "
                "for an ordinary task. Each task has a stable uid (TASK-0042). "
                "This is the same list Task Backlog.md renders — the file is "
                "a view of this."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(TASK_STATUSES)},
                    "statuses": {
                        "type": "array", "items": {"type": "string", "enum": list(TASK_STATUSES)},
                        "description": "Any subset of the status vocabulary. Takes precedence over 'status' and 'include_done'.",
                    },
                    "include_done": {
                        "type": "boolean", "default": False,
                        "description": "Deprecated alias for statuses=OPEN+['done']. Never includes someday/dropped.",
                    },
                    "priority": _priority,
                    "kind": _kind,
                    "severity": {"type": "string", "enum": list(SEVERITIES), "description": "Only bugs/features at this severity."},
                    "context": _context,
                    "energy": _energy,
                    "tag": {"type": "string", "description": "Exact tag, e.g. '#home'."},
                    "tag_prefix": {"type": "string", "description": f"Any tag under a namespace, e.g. '{CLAUDE_TAG_PREFIX}' for every Claude role."},
                    "project": {"type": "string"},
                    "program": {"type": "string", "description": "Program title (substring)."},
                    "queue": {"type": "string", "enum": list(QUEUES), "description": "'week' includes focus."},
                    "domain": {"type": "string", "description": "Domain name; matches explicit domain tags on the task, its project or its program, and a free tag naming the domain (#admin → Admin)."},
                    "text": {"type": "string"},
                    "due_before": {"type": "string", "format": "date-time"},
                    "overdue": {"type": "boolean", "default": False},
                    "completed_since": {
                        "type": "string", "format": "date-time",
                        "description": "Only rows completed at/after this time. Combine with statuses=['done'] to fetch e.g. today's closes without them competing with open rows for the limit.",
                    },
                    "owner": {"type": "string", "description": "Defaults to you. 'household' for everyone's; else 'me', a user id, or a users.name."},
                    "pending_for_me": {"type": "boolean", "default": False, "description": "Transfers awaiting your acceptance."},
                    "blocked_on_me": {"type": "boolean", "default": False, "description": "Open tasks you own that block someone else's open work."},
                    "handed_by_me": {"type": "boolean", "default": False, "description": "Loops you handed to someone else and they now own — to watch progress, not a blind transfer."},
                    "unfiled": {"type": "boolean", "default": False, "description": "Only tasks filed under no project."},
                    "routines": {
                        "type": "string", "enum": ["only", "exclude", "all"], "default": "all",
                        "description": "'only'/'exclude' rounds minted from a routine template.",
                    },
                    "include_unconfirmed": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Include unconfirmed (LLM-suggested, not yet reviewed) "
                            "tasks alongside confirmed ones. Default excludes them — "
                            "every other read (tasks_review, the rendered backlog, "
                            "the Loops app) does the same."
                        ),
                    },
                    "unconfirmed_only": {
                        "type": "boolean", "default": False,
                        "description": "Only unconfirmed tasks — the Suggested view. Takes precedence over include_unconfirmed.",
                    },
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
                "re-renders Task Backlog.md. kind: task (default) | bug | "
                "feature | chore — for household things, never for lios "
                "development items (file those as GitHub Issues on "
                "cograda/lios instead); severity applies to bugs/features. "
                "Unlike imported tasks, a task "
                "created here records a real created_at — the aging clock "
                "starts when a task is genuinely created, never at import. "
                "source must be one of: manual (default) | meeting | kickoff "
                "| seed | seed-whatsapp | voice | sweep | apple_reminders | "
                "routine | split | backlog_import | someday_import | "
                "delegated_import | legacy — an unknown value is refused "
                "rather than truncated or stored as free text. confirmed "
                "(default true) marks a human-initiated task active "
                "immediately; an extraction/suggestion pass should pass "
                "confirmed=false — the task is created but excluded from "
                "every active read until a human calls tasks_confirm."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string", "enum": list(TASK_STATUSES), "default": "next"},
                    "priority": _priority,
                    "kind": _kind,
                    "severity": _severity,
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
                    "source": {
                        "type": "string", "enum": list(TASK_SOURCES), "default": DEFAULT_TASK_SOURCE,
                    },
                    "confirmed": {
                        "type": "boolean", "default": True,
                        "description": (
                            "False for an unreviewed extraction/suggestion — the "
                            "task is created but stays out of every active read "
                            "until tasks_confirm is called."
                        ),
                    },
                    "requires_task": {
                        "type": "string",
                        "description": (
                            "uid of another task that must be done or dropped "
                            "first (e.g. TASK-0042) — a soft prerequisite, "
                            "not a hard gate. Must be visible to you (unowned "
                            "or owned by you)."
                        ),
                    },
                },
                "required": ["title"],
            },
            handler=tasks_add_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="tasks_update",
            description=(
                "Update one task by uid — title, priority, status, kind "
                "(task/bug/feature/chore), severity, project, "
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
                    "kind": _kind,
                    "severity": _severity,
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
                    "requires_task": {
                        "type": ["string", "null"],
                        "description": (
                            "uid of another task that must be done or dropped "
                            "first, or null to clear. Soft prerequisite, not a "
                            "hard gate. Must be visible to you."
                        ),
                    },
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
                "than the flagged count on its own. Also reports "
                "`unowned_open`: a count + list of open tasks with no "
                "owner_id, the hygiene gap a client rendering owner chips "
                "otherwise can't see. `owner` scopes the findings to one "
                "person's loops (unowned_open is never scoped); it defaults to "
                "your own, and 'household' is everyone's."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "flag": {"type": "string", "enum": list(FLAGS)},
                    "owner": {"type": "string", "description": "Defaults to you. 'household' for everyone's; else 'me', a user id, or a users.name."},
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
                "wrong rather than the link. `blocking_of` is the same "
                "relationship unranked and unlimited: every blocker uid "
                "mapped to the open tasks (uid+title) it is holding up. "
                "`owner` scopes all three to the blocked tasks one person "
                "owns; it defaults to your own, and 'household' is the whole graph."
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
                    "owner": {"type": "string", "description": "Defaults to you. 'household' for everyone's; else 'me', a user id, or a users.name."},
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
                                "kind": _kind,
                                "severity": _severity,
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
        CustomTool(
            name="tasks_notes",
            description=(
                "The dated notes on one task, oldest first — the running record "
                "of what happened and what was decided. Distinct from the task's "
                "description, which is its standing summary."
            ),
            input_schema={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=tasks_notes_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_note_add",
            description=(
                "Append one dated note to a task. Append-only: notes are never "
                "edited or deleted. Use this for what happened on a day — a call "
                "made, a decision, a finding — and tasks_update(description=) for "
                "the standing summary. Records a task_events row; does not change "
                "status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["uid", "body"],
            },
            handler=tasks_note_add_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="tasks_transfer",
            description=(
                "Request that a task change hands. This is a REQUEST, not a "
                "write ('TCP not UDP'): it sets a pending owner and a "
                "task_events row, but owner_id does not move until the "
                "pending owner calls tasks_accept. Calling this again before "
                "it is answered redirects or cancels the outstanding "
                "request. Sends the new pending owner a best-effort push "
                "naming who handed it over."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "to_user": {"type": "string", "description": "'me', a user id, or a users.name."},
                },
                "required": ["uid", "to_user"],
            },
            handler=tasks_transfer_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_accept",
            description=(
                "Accept a task transferred to you. Only callable by the "
                "pending owner (current_user_id()) — moves pending_owner_id "
                "into owner_id and records the acceptance. Sends the "
                "original requester a best-effort push."
            ),
            input_schema={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=tasks_accept_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="tasks_decline",
            description=(
                "Decline a task transferred to you (only callable by the "
                "pending owner; clears the pending state, owner_id never "
                "moves), OR dismiss an unconfirmed (LLM-suggested) task that "
                "has no pending transfer — sets it dropped rather than "
                "leaving it unconfirmed forever. An optional note is "
                "recorded as a dated comment; on a transfer decline the "
                "requester also gets a best-effort push naming who declined."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["uid"],
            },
            handler=tasks_decline_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="tasks_confirm",
            description=(
                "Confirm an unconfirmed (LLM-suggested) task, setting "
                "confirmed_at so it starts appearing in every active read — "
                "tasks_query's default, tasks_review, the rendered Task "
                "Backlog.md's main sections, the Loops app's default lenses. "
                "A no-op on an already-confirmed task. Use tasks_decline to "
                "dismiss a suggestion instead of confirming it."
            ),
            input_schema={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=tasks_confirm_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_nudge",
            description=(
                "Ask for a status update on a task someone else owns. Writes "
                "a task_events row (field='nudge') and a 'status update?' "
                "comment every time, and sends the owner a best-effort push "
                "— rate-limited so a repeat nudge within the cooldown "
                "updates the ledger without ringing the phone again. The "
                "row's `nudges` count and `last_nudged_at` surface this."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["uid"],
            },
            handler=tasks_nudge_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="tasks_users",
            description="The household's active users — for an owner/'hand to…' picker.",
            input_schema={"type": "object", "properties": {}},
            handler=tasks_users_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_whoami",
            description=(
                "The caller's own id, name and display_name. Lets a client gate "
                "owner-only UI (e.g. an Accept/Decline control, or a 'hand to "
                "me' option) against reality instead of assuming who is asking; "
                "server-side writes are unaffected and enforce ownership "
                "regardless of what a client shows."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=tasks_whoami_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_absence_alerts",
            description=(
                "Open absence-detection findings: a routine whose window "
                "closed with no round completed, a 'waiting' item past due "
                "with no note, or a snag unanswered for weeks. Each fires "
                "exactly once (a persisted dedup key, not re-alerted on "
                "every tick) and clears itself once the underlying gap is "
                "closed. household=true widens the scope to every open "
                "finding regardless of owner (the default is 'mine plus "
                "unowned', same shape as system_alerts axis 5)."
            ),
            input_schema={
                "type": "object",
                "properties": {"household": {"type": "boolean", "default": False}},
            },
            handler=tasks_absence_alerts_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        *loops.loops_tools(),
        *dupes.dupes_tools(),
        *routines.routines_tools(),
        *intake.intake_tools(),
    ]
