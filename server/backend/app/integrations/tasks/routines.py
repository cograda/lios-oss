"""Routines and rounds — the recurring class of loop (chunk E3, 2026-09-03).

Vocabulary ruled by Alex, do not re-open: a **loop** is anything with a
definition of done; a **routine** is a recurring loop template that never
closes itself; a **round** is one occurrence of a routine, and it closes.

**A round IS a `tasks` row.** Routines are the template; each round is a real
task with `tasks.routine_id` set. Rounds get every existing lens, queue,
block, comment, history and accept/transfer semantic for free — there is
deliberately no separate occurrence table.

**Just-in-time minting.** Only the next round exists as an open row at any
time. `mint_round()` creates it; the scheduler (`run_tick`, every 15 minutes,
see `manifest.py`) mints it when a fixed/window routine's schedule fires, and
`tasks_complete_handler` (see `tools.py`) mints an interval routine's next
round the moment its current one closes. Never a year of rows up front.

**Skipped, not left open.** If a round is still open when the next is due,
`tick_once()` closes it `dropped` with a `field="skip"` `TaskEvent`, then
mints the next. A skip is the signal a routine is failing, not a state the
ledger stays in silently — `routines_list` surfaces a 30-day skip count for
exactly this reason.

**Window-kind schedules are a stopgap in this chunk.** The schema stores a
named window (e.g. `"bedtime"`) but resolution — mapping names to actual
times of day — is out of scope here. Every window routine is treated as
"daily at `WINDOW_STOPGAP_HOUR`", regardless of the name stored. This is
recorded at every call site that does it (`_next_window_occurrence`), not
just here, since that is the trap a future reader is most likely to trip on.

**Hand-over is two different things.** Handing over ONE round is the
existing `tasks_transfer`/`_accept`/`_decline` on that round's own `tasks`
row — nothing new. Handing over the ROUTINE itself — who gets every FUTURE
round — is `routines_transfer`/`_accept`/`_decline` here, the identical
request/accept shape ("TCP not UDP") applied to `Routine.pending_owner_id`
instead of `Task.pending_owner_id`. Accepting a routine also moves the
*current open round's* owner, so a hand-over doesn't leave the round in
flight pointed at the old owner.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from dateutil.rrule import rrulestr
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.tasks import loops
from app.integrations.tasks.loops import _find_program, _find_project  # noqa: PLC0415 - same package
from app.integrations.tasks.models import (
    PREREQUISITE_SATISFIED_STATUSES, SCHEDULE_KINDS, Routine, RoutineStep, Task, TaskEvent,
)
from app.models.users import User
from app.services.text import escape_ilike
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import iso_or_none

logger = logging.getLogger(__name__)

OPEN_TASK_STATUSES = loops.OPEN_TASK_STATUSES  # ("inbox", "next", "waiting", "scheduled")

# The stopgap for `schedule_kind == "window"` — see the module docstring.
# Every named window resolves to this one hour, UTC, regardless of the name
# stored in `schedule_spec`. Naming windows for real (morning/bedtime/etc as
# distinct times) is future work, deliberately not this chunk's.
WINDOW_STOPGAP_HOUR = 20

_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


# ─── uids ──────────────────────────────────────────────────────────────────


def _resolve_user(session: Session, value: str) -> int:
    """Same shape as `tools._resolve_user` (duplicated rather than imported —
    `tools.py` imports this module, so the reverse import would cycle)."""
    if value == "me":
        return current_user_id()
    if str(value).isdigit():
        return int(value)
    user = session.query(User).filter(User.name == value).one_or_none()
    if user is None:
        names = sorted(u.name for u in session.query(User).all())
        raise ValueError(f"Unknown user: {value}. Existing users: {', '.join(names)}")
    return user.id


def _find_routine(session: Session, ref: str) -> Routine:
    r = session.query(Routine).filter(
        (Routine.uid == ref) | (Routine.title.ilike(escape_ilike(ref)))
    ).one_or_none()
    if r is None:
        raise ValueError(
            f"Unknown routine: {ref}. Existing routines: "
            + (", ".join(sorted(x.title for x in session.query(Routine).all())) or "(none)")
        )
    return r


# ─── schedule math ─────────────────────────────────────────────────────────


def _parse_iso_duration(spec: str) -> timedelta:
    """A useful subset of ISO-8601 durations: Y/M/W/D/H/Min/S, all optional,
    at least one present. Y is approximated as 365 days and M as 30 — both
    documented here because an interval routine's whole point is a cadence a
    person picked, not calendar precision, and the schema's own example
    (`P28D`) never needs either approximation."""
    m = _ISO_DURATION_RE.match((spec or "").strip())
    if not m or not any(m.groups()):
        raise ValueError(
            f"Not a recognised ISO-8601 duration: {spec!r}. Examples: 'P28D' "
            "(28 days), 'P7D' (a week), 'P1M' (~30 days)."
        )
    g = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    return timedelta(
        days=g["years"] * 365 + g["months"] * 30 + g["weeks"] * 7 + g["days"],
        hours=g["hours"], minutes=g["minutes"], seconds=g["seconds"],
    )


_RRULE_ANCHOR = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _next_fixed_occurrence(spec: str, after: datetime) -> datetime:
    """The next time `spec` (an RRULE string, e.g.
    'FREQ=WEEKLY;BYDAY=TU;BYHOUR=19') fires strictly after `after`."""
    try:
        rule = rrulestr(f"RRULE:{spec}", dtstart=_RRULE_ANCHOR)
    except ValueError as e:
        raise ValueError(f"Not a valid RRULE: {spec!r} ({e})") from e
    nxt = rule.after(after, inc=False)
    if nxt is None:
        raise ValueError(f"RRULE {spec!r} produces no occurrence after {after.isoformat()}")
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return nxt


def _next_window_occurrence(after: datetime) -> datetime:
    """See WINDOW_STOPGAP_HOUR — the named window is not yet resolved to a
    real time of day, so every window is 'daily at this UTC hour'."""
    after = after.astimezone(timezone.utc)
    candidate = after.replace(hour=WINDOW_STOPGAP_HOUR, minute=0, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def next_due_after(routine: Routine, after: datetime) -> datetime:
    """The next time this routine's schedule fires, strictly after `after`.

    For an interval routine `after` should be the last close (or the
    routine's own creation, if it has never closed) — the duration is
    *since last close*, not a wall-clock recurrence.
    """
    if routine.schedule_kind == "fixed":
        return _next_fixed_occurrence(routine.schedule_spec, after)
    if routine.schedule_kind == "window":
        return _next_window_occurrence(after)
    if routine.schedule_kind == "interval":
        return after + _parse_iso_duration(routine.schedule_spec)
    raise ValueError(f"Unknown schedule_kind: {routine.schedule_kind!r}")


def validate_schedule(schedule_kind: str, schedule_spec: str) -> None:
    """Raise now, at add/update time, rather than silently failing every
    15 minutes in the scheduler. Calls `next_due_after` against a throwaway
    Routine-shaped object to reuse the real parsing/validation path."""
    if schedule_kind not in SCHEDULE_KINDS:
        raise ValueError(f"schedule_kind must be one of {SCHEDULE_KINDS}")
    if not (schedule_spec or "").strip():
        raise ValueError("schedule_spec is required")
    probe = Routine(schedule_kind=schedule_kind, schedule_spec=schedule_spec)
    next_due_after(probe, datetime.now(timezone.utc))


# ─── rounds ────────────────────────────────────────────────────────────────


def current_round(session: Session, routine: Routine) -> Task | None:
    """The one open round for this routine, or None. 'Open' means the
    invariant is intact; more than one would be a bug elsewhere, so this
    takes the most recently created if it ever happens rather than raising —
    `tick_once` treats that as already-satisfied, not as a fresh mint."""
    return (
        session.query(Task)
        .filter(Task.routine_id == routine.id, Task.status.in_(OPEN_TASK_STATUSES))
        .order_by(Task.id.desc())
        .first()
    )


def _routine_steps(session: Session, routine: Routine) -> list[RoutineStep]:
    # ⚠️ Flush first: this session's factory is `autoflush=False` (coglib's
    # default), so a step added moments ago in the same call (routines_add,
    # routines_update) is otherwise invisible to this SELECT — the round
    # would mint with no checklist even though the step rows exist, staged,
    # in the very same transaction.
    session.flush()
    return (
        session.query(RoutineStep)
        .filter(RoutineStep.routine_id == routine.id)
        .order_by(RoutineStep.ord)
        .all()
    )


def mint_round(session: Session, routine: Routine, *, due: datetime | None = None) -> Task:
    """Create the next round. `due` is when it becomes actionable — if in the
    future the round starts `scheduled` (GTD's defer_until semantics already
    mean exactly this: not actionable yet); `tick_once` flips it to `next`
    once due. If `due` is None or already past, it starts `next` directly.

    Steps are copied into the round's description as a checklist — chosen
    over a first TaskComment because a round's description is already the
    field every existing lens/render shows inline (see `render.py`), and a
    checklist is meaningful markdown there; a comment would need a second
    round-trip (`tasks_notes`) to see at all.

    TODO (lios#156, C2 "runs with prerequisites", deferred by design): this
    does not resolve a per-`RoutineStep` default into a concrete
    `requires_task_id` for the newly minted round. The proposal allowed
    shipping the instance-level field alone if the default-resolution
    logic wasn't a small addition — it isn't, because "which task
    represents THIS cycle's occurrence of the prerequisite routine" needs
    its own cross-routine lookup (the prerequisite routine's own current
    round, which may not exist yet if it mints later in the day). Until
    that lands, every newly minted round starts with `requires_task_id`
    NULL; set it explicitly per cycle via `routines_update`'s
    `requires_task` (applies to the current open round) or `tasks_update`.
    """
    now = datetime.now(timezone.utc)
    steps = _routine_steps(session, routine)
    description = routine.done_when or None
    if steps:
        checklist = "\n".join(f"- [ ] {s.text}" for s in steps)
        description = f"{description}\n\n{checklist}" if description else checklist

    actionable_now = due is None or due <= now
    round_task = Task(
        uid=loops.next_uid(session, Task, "TASK"),
        title=routine.title[:300],
        description=description,
        status="next" if actionable_now else "scheduled",
        owner_id=routine.default_owner_id,
        project_id=routine.project_id,
        routine_id=routine.id,
        defer_until=None if actionable_now else due,
        source="routine",
        # lios#224: a routine minting its next round is a system default
        # action, not an LLM suggestion — confirmed at mint time.
        confirmed_at=now,
        created_at=now,
        sort_order=(session.query(Task).count() + 1) * 1000,
    )
    session.add(round_task)
    session.flush()
    session.add(TaskEvent(
        task_id=round_task.id, routine_id=routine.id,
        from_status=None, to_status=round_task.status,
        note=f"minted from {routine.uid}",
    ))
    # Deliberately NOT enqueued for embedding/duplicate detection — see
    # `dupes.py`'s and `review.py`'s exclusion of `routine_id IS NOT NULL`
    # rows: a recurring round would otherwise permanently "duplicate" its own
    # predecessor, every single cycle.
    return round_task


def skip_round(session: Session, round_task: Task, routine: Routine, *, reason: str) -> None:
    """Close a still-open round as failed-to-complete-in-time. Distinct from
    a normal completion: `field="skip"` marks it so `routines_list`'s 30-day
    skip count (and a future dashboard) can tell 'finished' from 'replaced
    because it ran out the clock' without re-deriving it from prose."""
    old_status = round_task.status
    round_task.status = "dropped"
    round_task.completed_at = None
    session.add(TaskEvent(
        task_id=round_task.id, routine_id=routine.id,
        from_status=old_status, to_status="dropped", field="skip",
        note=reason,
    ))


def _round_overdue(routine: Routine, round_task: Task, now: datetime) -> bool:
    """Is the NEXT round already due while this one is still open?"""
    if routine.schedule_kind == "interval":
        anchor = round_task.created_at or now
        due_for_next = anchor + _parse_iso_duration(routine.schedule_spec)
    else:
        anchor = round_task.defer_until or round_task.created_at or now
        due_for_next = next_due_after(routine, anchor)
    return now >= due_for_next


def tick_once(session: Session) -> dict:
    """The scheduler entry point's synchronous body (see `run_tick`).
    Idempotent: run it twice in a row with nothing else happening and the
    second call finds nothing to do, because minting/flipping resets the
    anchors the overdue check reads.
    """
    now = datetime.now(timezone.utc)
    minted: list[str] = []
    skipped: list[str] = []
    flipped: list[str] = []

    routines = session.query(Routine).filter(Routine.active.is_(True)).all()
    for routine in routines:
        round_task = current_round(session, routine)

        if round_task is None:
            # Invariant repair — should only happen after reactivation missed
            # its own immediate mint, or a manual data fix. Mint due now.
            new = mint_round(session, routine, due=now)
            minted.append(new.uid)
            continue

        if round_task.status == "scheduled" and round_task.defer_until and round_task.defer_until <= now:
            round_task.status = "next"
            session.add(TaskEvent(
                task_id=round_task.id, routine_id=routine.id,
                from_status="scheduled", to_status="next", note="due",
            ))
            flipped.append(round_task.uid)

        if _round_overdue(routine, round_task, now):
            skip_round(
                session, round_task, routine,
                reason=f"{routine.uid}: next round due, previous round still open",
            )
            skipped.append(round_task.uid)
            next_due = now if routine.schedule_kind == "interval" else next_due_after(routine, now)
            new = mint_round(session, routine, due=next_due)
            minted.append(new.uid)

    session.commit()
    return {"minted": minted, "skipped": skipped, "flipped_to_next": flipped}


def mint_next_on_complete(session: Session, round_task: Task) -> Task | None:
    """Called from `tasks_complete_handler` (tools.py). Interval routines
    mint their next round the instant this one closes — fixed/window
    routines wait for `run_tick`, since their next occurrence is a wall-clock
    fact this completion doesn't change.

    ⚠️ This session's factory is `autoflush=False` (coglib's default), so
    `round_task.status = "done"` (set moments ago by `_set_status`) is not
    yet visible to a fresh SELECT until flushed. Without the flush,
    `current_round()` below finds the just-completed round still reading
    "next" in the database and concludes the invariant already holds —
    silently skipping the mint every time. `session.flush()` sends pending
    changes to Postgres (still inside the caller's open transaction; nothing
    is committed here) so this query sees them.
    """
    if round_task.routine_id is None:
        return None
    session.flush()
    routine = session.query(Routine).filter(Routine.id == round_task.routine_id).one_or_none()
    if routine is None or not routine.active or routine.schedule_kind != "interval":
        return None
    if current_round(session, routine) is not None:
        return None  # invariant already holds — nothing to do
    now = datetime.now(timezone.utc)
    return mint_round(session, routine, due=next_due_after(routine, now))


# ─── cron entry point ──────────────────────────────────────────────────────


async def run_tick() -> None:
    """Cron entry point (see `manifest.py::background_tasks`), every 15
    minutes — same shape as `notifications.sweep.run_sweep`. Writes
    SyncState under this integration's own name so a broken tick shows up on
    the dashboard rather than silently starving every routine in the house.

    Absence detection (R5, Wave 2) runs here too, deliberately as a second
    step of this SAME tick rather than a sibling scheduler entry -- the
    brief for that chunk explicitly said not to add a second scheduler. It
    runs AFTER tick_once(), not before: `absence.missed_routine_windows`
    reads the most recent close event for each routine, so it needs this
    tick's own skip (if any) already written to see it in the same cycle it
    happens, rather than one tick later. See `absence.py`'s module docstring
    for the full design (why this doesn't flow through `system_alerts`, and
    the dedup/resolve lifecycle). A broken absence pass must not stop rounds
    minting or vice versa -- caught and logged independently, never allowed
    to skip the SyncState write below."""
    import asyncio

    from app.db import get_db
    from app.scheduler import _update_sync_state

    def _run() -> dict:
        db = get_db()
        with db.session() as session:
            result = tick_once(session)
            try:
                from app.integrations.tasks import absence  # noqa: PLC0415 - avoid import cycle
                result["absence"] = absence.reconcile_alerts(session, datetime.now(timezone.utc))
            except Exception:
                logger.exception("absence reconcile failed")
                result["absence"] = None
            return result

    # S5.1: current_run() is the `runs` ledger row `app.scheduler`'s generic
    # wrap opened for this execution (None outside it, e.g. a direct unit
    # test call — touched() is skipped then, never raises). Reported
    # regardless of outcome below, so a failed tick's row still shows what
    # was minted/skipped before the failure, if anything was.
    from app.services.runs import current_run

    run = current_run()

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        logger.exception("routines tick failed")
        _update_sync_state("tasks", status="error", error=str(exc)[:200], trigger="routines_tick")
        return

    if run is not None:
        run.touched(
            minted=result["minted"],
            skipped=result["skipped"],
            flipped_to_next=result["flipped_to_next"],
            absence=result["absence"],
        )

    if result["minted"] or result["skipped"]:
        # Scheduler context: no bound user, so `_render` would raise the
        # moment a round is first minted (latent until 2026-09-04, when the
        # reminders inlet's identical call failed on its first tick).
        with get_db().session() as session:
            from app.integrations.tasks.tools import render_all_vaults  # noqa: PLC0415 - avoid import cycle
            render_all_vaults(session)

    logger.info("routines tick: %s", result)
    _update_sync_state("tasks", status="ok", trigger="routines_tick")


# ─── handlers ──────────────────────────────────────────────────────────────


def _round_prerequisite(session: Session, round_task: Task) -> dict | None:
    """C2 "runs with prerequisites" (lios#156): the computed
    `{"text", "satisfied"} | null` for one round — never a raw task id (that
    is `current_round.requires_task_id`, alongside it, in the tool payload
    only). `satisfied` is true once the referenced task is done or dropped
    — see `PREREQUISITE_SATISFIED_STATUSES`'s docstring for why a skip
    counts as satisfied rather than stuck."""
    if round_task.requires_task_id is None:
        return None
    ref = session.query(Task).filter(Task.id == round_task.requires_task_id).one_or_none()
    if ref is None:
        return None
    return {"text": ref.title, "satisfied": ref.status in PREREQUISITE_SATISFIED_STATUSES}


def _routine_row(session: Session, routine: Routine, *, dnames: dict | None = None) -> dict:
    dnames = dnames if dnames is not None else loops.domain_names(session)
    round_task = current_round(session, routine)
    # A round closes three ways: completed (field is None, to_status="done"),
    # deactivated (field is None, to_status="dropped"), or skipped (field=
    # "skip", to_status="dropped") — all three count as "the routine closed a
    # round", so all three are read here.
    # ⚠️ `field IN (NULL, 'skip')` is not the same test: SQL's `x IN (NULL, …)`
    # evaluates to NULL (never true) for a NULL-field row, so `.in_()` would
    # silently drop every ordinary completion from this query. Use `or_`.
    last_close = (
        session.query(TaskEvent)
        .filter(
            TaskEvent.routine_id == routine.id,
            or_(TaskEvent.field.is_(None), TaskEvent.field == "skip"),
            TaskEvent.to_status.in_(("done", "dropped")),
        )
        .order_by(TaskEvent.at.desc())
        .first()
    )
    since = datetime.now(timezone.utc) - timedelta(days=30)
    skip_count = (
        session.query(TaskEvent)
        .filter(TaskEvent.routine_id == routine.id, TaskEvent.field == "skip", TaskEvent.at >= since)
        .count()
    )
    return {
        "uid": routine.uid,
        "title": routine.title,
        "active": routine.active,
        "done_when": routine.done_when,
        "schedule_kind": routine.schedule_kind,
        "schedule_spec": routine.schedule_spec,
        "default_owner_id": routine.default_owner_id,
        "pending_owner_id": routine.pending_owner_id,
        "pending_since": iso_or_none(routine.transfer_requested_at),
        # Who asked (see tools._row's pending_from_id) — the actor of the
        # latest transfer-request event on this routine; NULL unless pending.
        "pending_from_id": _requester_of_pending_transfer(session, routine),
        "domains": dnames.get(("routine", routine.id), []),
        "steps": [s.text for s in _routine_steps(session, routine)],
        "current_round": (
            {
                "uid": round_task.uid,
                "status": round_task.status,
                "due": iso_or_none(round_task.defer_until),
                # C2 "runs with prerequisites" (lios#156). Raw id alongside
                # the computed sentence is fine here (tool payload, not the
                # panel path — see tools._rows for the same pairing).
                "requires_task_id": round_task.requires_task_id,
                "prerequisite": _round_prerequisite(session, round_task),
            }
            if round_task else None
        ),
        "next_due": iso_or_none(round_task.defer_until) if round_task else None,
        "last_closed_at": iso_or_none(last_close.at) if last_close else None,
        "skips_last_30d": skip_count,
    }


def routines_add_handler(session: Session, args: dict) -> str:
    title = (args.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    done_when = (args.get("done_when") or "").strip()
    if not done_when:
        raise ValueError(
            "A routine needs a definition of done (`done_when`) — copied onto every round it mints."
        )
    schedule_kind = args.get("schedule_kind")
    schedule_spec = args.get("schedule_spec")
    validate_schedule(schedule_kind, schedule_spec)

    owner_id = _resolve_user(session, args.get("default_owner") or "me")
    program_id = _find_program(session, args["program"]).id if args.get("program") else None
    project_id = _find_project(session, args["project"]).id if args.get("project") else None

    routine = Routine(
        uid=loops.next_uid(session, Routine, "RTN"),
        title=title[:300],
        done_when=done_when,
        default_owner_id=owner_id,
        program_id=program_id,
        project_id=project_id,
        schedule_kind=schedule_kind,
        schedule_spec=schedule_spec,
        active=True,
    )
    session.add(routine)
    session.flush()

    for i, text in enumerate(args.get("steps") or [], start=1):
        text = (text or "").strip()
        if text:
            session.add(RoutineStep(routine_id=routine.id, ord=i, text=text))

    if args.get("domains"):
        loops.set_domain_tags(session, args["domains"], routine=routine)

    first_round = mint_round(session, routine, due=datetime.now(timezone.utc))

    # C2 "runs with prerequisites" (lios#156). Sets the prerequisite on THIS
    # round only — see the module-level TODO by mint_round for why a
    # routine-template-level default isn't resolved automatically yet.
    if args.get("requires_task"):
        from app.integrations.tasks.tools import _field_event, _resolve_requires_task  # noqa: PLC0415
        first_round.requires_task_id = _resolve_requires_task(
            session, args["requires_task"], first_round,
        )
        _field_event(session, first_round, "requires_task_id", None, first_round.requires_task_id)

    session.commit()
    from app.integrations.tasks.tools import _render  # noqa: PLC0415
    _render(session)
    return json.dumps({
        "created": _routine_row(session, routine),
        "first_round": first_round.uid,
    })


def routines_list_handler(session: Session, args: dict) -> str:
    q = session.query(Routine)
    if not args.get("include_inactive"):
        q = q.filter(Routine.active.is_(True))
    routines = q.order_by(Routine.id).all()
    dnames = loops.domain_names(session)
    return json.dumps({
        "count": len(routines),
        "routines": [_routine_row(session, r, dnames=dnames) for r in routines],
    })


def routines_update_handler(session: Session, args: dict) -> str:
    routine = _find_routine(session, args["routine"])

    for field in ("title", "done_when"):
        if field in args:
            setattr(routine, field, args[field])
    if "schedule_kind" in args or "schedule_spec" in args:
        kind = args.get("schedule_kind", routine.schedule_kind)
        spec = args.get("schedule_spec", routine.schedule_spec)
        validate_schedule(kind, spec)
        routine.schedule_kind, routine.schedule_spec = kind, spec
    if "program" in args:
        routine.program_id = None if args["program"] is None else _find_program(session, args["program"]).id
    if "project" in args:
        routine.project_id = None if args["project"] is None else _find_project(session, args["project"]).id
    if "domains" in args:
        loops.set_domain_tags(session, args["domains"] or [], routine=routine)
    if "steps" in args:
        session.query(RoutineStep).filter(RoutineStep.routine_id == routine.id).delete(synchronize_session=False)
        for i, text in enumerate(args["steps"] or [], start=1):
            text = (text or "").strip()
            if text:
                session.add(RoutineStep(routine_id=routine.id, ord=i, text=text))

    if "active" in args:
        new_active = bool(args["active"])
        if routine.active and not new_active:
            # Deactivating drops the open round — NOT a skip (that signal
            # means "the routine is failing"; this is "we stopped it on
            # purpose") — and mints nothing next, since it is now inactive.
            round_task = current_round(session, routine)
            if round_task is not None:
                old = round_task.status
                round_task.status = "dropped"
                round_task.completed_at = None
                session.add(TaskEvent(
                    task_id=round_task.id, routine_id=routine.id,
                    from_status=old, to_status="dropped",
                    note="routine deactivated",
                ))
        elif not routine.active and new_active and current_round(session, routine) is None:
            # Reactivating restores the just-in-time invariant immediately
            # rather than waiting up to 15 minutes for the next tick.
            mint_round(session, routine, due=datetime.now(timezone.utc))
        routine.active = new_active

    if "requires_task" in args:
        # C2 "runs with prerequisites" (lios#156). Declared on the round
        # INSTANCE, not the template — see models.py's Task.requires_task_id
        # docstring — so this sets (or, with null, clears) it on the
        # routine's CURRENT open round only. There is deliberately no
        # per-routine default yet (see mint_round's TODO); each new round
        # starts with no prerequisite until this is called again.
        round_task = current_round(session, routine)
        if round_task is None:
            raise ValueError(f"{routine.uid} has no open round to set a prerequisite on.")
        from app.integrations.tasks.tools import _field_event, _resolve_requires_task  # noqa: PLC0415
        ref = args["requires_task"]
        new_id = None if ref is None else _resolve_requires_task(session, ref, round_task)
        _field_event(session, round_task, "requires_task_id", round_task.requires_task_id, new_id)
        round_task.requires_task_id = new_id

    session.commit()
    from app.integrations.tasks.tools import _render  # noqa: PLC0415
    _render(session)
    return json.dumps({"updated": _routine_row(session, routine)})


def routines_skip_handler(session: Session, args: dict) -> str:
    routine = _find_routine(session, args["routine"])
    round_task = current_round(session, routine)
    if round_task is None:
        raise ValueError(f"{routine.uid} has no open round to skip.")
    skip_round(session, round_task, routine, reason=(args.get("note") or "manual skip"))
    now = datetime.now(timezone.utc)
    next_due = now if routine.schedule_kind == "interval" else next_due_after(routine, now)
    new_round = mint_round(session, routine, due=next_due)
    session.commit()
    from app.integrations.tasks.tools import _render  # noqa: PLC0415
    _render(session)
    return json.dumps(
        {"skipped": round_task.uid, "minted": new_round.uid}
    )


# `TaskEvent.to_status` is NOT NULL. Routine-level events have no task
# status to carry (task_id is None), so this sentinel fills the column —
# the same shape as render.py's RENDER_STATUS/SWEEP_STATUS. It never
# surfaces in `tasks_history`'s output: that handler reads old_value/
# new_value, not to_status, whenever `field` is set (which it always is here).
ROUTINE_EVENT_STATUS = "routine"


def _requester_of_pending_transfer(session: Session, routine: Routine) -> int | None:
    """Who asked for the routine's outstanding hand-over — the actor of its
    latest 'requested' transfer event (a redirect counts). Mirrors
    tools._requesters_of_pending_transfers for tasks; keyed on `routine_id`
    because routine-level events carry no task_id."""
    if routine.pending_owner_id is None:
        return None
    event = (
        session.query(TaskEvent.actor_id)
        .filter(TaskEvent.routine_id == routine.id, TaskEvent.task_id.is_(None))
        .filter(TaskEvent.field == "transfer", TaskEvent.note.ilike("requested%"))
        .order_by(TaskEvent.at.desc(), TaskEvent.id.desc())
        .first()
    )
    return event[0] if event else None


def _transfer_event(session: Session, routine: Routine, note: str, *, old: int | None, new: int | None) -> None:
    # `actor_id` was missing here until 2026-09-06, so a routine hand-over
    # recorded *that* it was requested but not by whom — the row's
    # `pending_from_id` below reads it, and is NULL for those older events.
    session.add(TaskEvent(
        task_id=None, routine_id=routine.id, from_status=None, to_status=ROUTINE_EVENT_STATUS,
        actor_id=current_user_id(), field="transfer",
        old_value=str(old) if old is not None else None,
        new_value=str(new) if new is not None else None,
        note=note,
    ))


def routines_transfer_handler(session: Session, args: dict) -> str:
    routine = _find_routine(session, args["routine"])
    to_id = _resolve_user(session, args["to_user"])
    old_pending = routine.pending_owner_id
    routine.pending_owner_id = to_id
    routine.transfer_requested_at = datetime.now(timezone.utc)
    _transfer_event(
        session, routine,
        "requested" if old_pending is None else "requested (redirected)",
        old=old_pending, new=to_id,
    )
    session.commit()
    if to_id != current_user_id():
        from app.integrations.tasks.tools import _display_name, _notify  # noqa: PLC0415
        requester = _display_name(session, current_user_id())
        _notify(f"lios: {requester} handed you a routine", f"{routine.uid}: {routine.title}", to_id)
    return json.dumps({"transferred": _routine_row(session, routine)})


def routines_accept_handler(session: Session, args: dict) -> str:
    routine = _find_routine(session, args["routine"])
    me = current_user_id()
    if routine.pending_owner_id != me:
        raise ValueError(f"{routine.uid} has no transfer pending for you to accept.")
    old_owner = routine.default_owner_id
    routine.default_owner_id = me
    routine.pending_owner_id = None
    routine.transfer_requested_at = None
    # The open round's owner follows the routine.
    round_task = current_round(session, routine)
    if round_task is not None:
        round_task.owner_id = me
    _transfer_event(session, routine, "accepted", old=old_owner, new=me)
    session.commit()
    from app.integrations.tasks.tools import _render  # noqa: PLC0415
    _render(session)
    return json.dumps({"accepted": _routine_row(session, routine)})


def routines_decline_handler(session: Session, args: dict) -> str:
    routine = _find_routine(session, args["routine"])
    me = current_user_id()
    if routine.pending_owner_id != me:
        raise ValueError(f"{routine.uid} has no transfer pending for you to decline.")
    pending = routine.pending_owner_id
    routine.pending_owner_id = None
    routine.transfer_requested_at = None
    note = (args.get("note") or "").strip()
    _transfer_event(session, routine, "declined" + (f": {note}" if note else ""), old=pending, new=None)
    session.commit()
    return json.dumps({"declined": _routine_row(session, routine)})


# ─── MCP tools ─────────────────────────────────────────────────────────────


def routines_tools() -> list[dict]:
    _domains = {
        "type": "array", "items": {"type": "string"},
        "description": "Domain names (household areas). Replaces the current set; must already exist.",
    }
    _schedule_kind = {"type": "string", "enum": list(SCHEDULE_KINDS)}
    return [
        CustomTool(
            name="routines_add",
            description=(
                "Create a routine (a recurring loop template) and immediately "
                "mint its first round. schedule_kind is 'fixed' (schedule_spec "
                "is an RRULE, e.g. 'FREQ=WEEKLY;BYDAY=TU;BYHOUR=19'), 'interval' "
                "(schedule_spec is an ISO-8601 duration since the previous round "
                "closed, e.g. 'P28D'), or 'window' (a named day window — "
                "resolution isn't built yet, so it's currently treated as daily "
                "at a fixed hour). Steps become a checklist in every round's "
                "description. Re-renders Task Backlog.md."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "done_when": {"type": "string", "description": "Copied onto every round's description."},
                    "schedule_kind": _schedule_kind,
                    "schedule_spec": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "default_owner": {"type": "string", "description": "'me', a user id, or a users.name.", "default": "me"},
                    "program": {"type": "string"},
                    "project": {"type": "string"},
                    "domains": _domains,
                    "requires_task": {
                        "type": "string",
                        "description": (
                            "uid of another task that must be done or dropped "
                            "first, applied to the FIRST round only (soft "
                            "prerequisite, not a hard gate). Must be visible "
                            "to you."
                        ),
                    },
                },
                "required": ["title", "done_when", "schedule_kind", "schedule_spec"],
            },
            handler=routines_add_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="routines_list",
            description=(
                "List routines (active only by default) with next_due, "
                "last_closed_at, a 30-day skip count, and the uid of the "
                "current open round."
            ),
            input_schema={
                "type": "object",
                "properties": {"include_inactive": {"type": "boolean", "default": False}},
            },
            handler=routines_list_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="routines_update",
            description=(
                "Update a routine's title, done_when, schedule, program/"
                "project, domains, steps, or active flag. Deactivating drops "
                "the current open round (event reason 'routine deactivated', "
                "NOT a skip) and mints nothing further; reactivating mints "
                "immediately rather than waiting for the next scheduler tick. "
                "requires_task sets/clears a soft prerequisite on the "
                "CURRENT open round only (a round is day-specific; there is "
                "no per-routine default yet — set it again each cycle)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "routine": {"type": "string", "description": "Existing title or uid (RTN-0001)."},
                    "title": {"type": "string"},
                    "done_when": {"type": "string"},
                    "schedule_kind": _schedule_kind,
                    "schedule_spec": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "program": {"type": ["string", "null"]},
                    "project": {"type": ["string", "null"]},
                    "domains": _domains,
                    "active": {"type": "boolean"},
                    "requires_task": {
                        "type": ["string", "null"],
                        "description": (
                            "uid of another task that must be done or dropped "
                            "first, or null to clear. Applies to the current "
                            "open round only. Must be visible to you."
                        ),
                    },
                },
                "required": ["routine"],
            },
            handler=routines_update_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="routines_skip",
            description=(
                "Explicitly skip the current round of a routine: closes it "
                "dropped with a skipped event and mints the next round "
                "immediately. Use when a round is known to be missed rather "
                "than waiting for the scheduler to notice."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "routine": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["routine"],
            },
            handler=routines_skip_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="routines_transfer",
            description=(
                "Request that a routine (every FUTURE round, not just the "
                "current one) change hands. A REQUEST, not a write — sets a "
                "pending owner; owner does not move until routines_accept. "
                "To hand over just the current round, use tasks_transfer on "
                "its own uid instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "routine": {"type": "string"},
                    "to_user": {"type": "string", "description": "'me', a user id, or a users.name."},
                },
                "required": ["routine", "to_user"],
            },
            handler=routines_transfer_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="routines_accept",
            description=(
                "Accept a routine transferred to you. Only callable by the "
                "pending owner. Moves default ownership AND the current open "
                "round's owner to you."
            ),
            input_schema={
                "type": "object",
                "properties": {"routine": {"type": "string"}},
                "required": ["routine"],
            },
            handler=routines_accept_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
        CustomTool(
            name="routines_decline",
            description="Decline a routine transferred to you. Only callable by the pending owner.",
            input_schema={
                "type": "object",
                "properties": {
                    "routine": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["routine"],
            },
            handler=routines_decline_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
    ]
