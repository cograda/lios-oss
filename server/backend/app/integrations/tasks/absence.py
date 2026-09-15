"""Absence detection — alert on silence (R5, Wave 2, 2026-09-04).

The hard problem in family logistics is the thing that *didn't* happen: a
repeat prescription not reordered, a car service overdue, a snag unanswered
for three weeks. The cadence data already exists in this package and in
`snags` — nothing alerted on it going quiet. Three checks:

  - `missed_routine_windows` — a routine whose window closed with no round
    completed (`routines.py`'s own `tick_once` already skips the round and
    mints the next one silently; this surfaces that it happened).
  - `stale_waiting` — a `waiting` task past its due date with no note added
    since.
  - `unanswered_snags` — a snag sitting `open`/`reported` for N weeks with no
    trade response, read through `snags`' facade (`snags.query`).

Each returns a list of `Finding`s: pure, read-only, and safe to call as often
as anyone likes — they derive the current truth from the ledger each time,
they don't remember anything themselves.

**Persistence lives separately, in `reconcile_alerts`.** A finding's identity
is `(kind, ref, since)` — `_dedup_key` turns that into
`absence:<kind>:<ref>:<since-iso>`, and `AbsenceAlert` (models.py) is the one
open row per key (partial unique index, same shape as
`notifications.NotificationSend`). `reconcile_alerts` diffs the current
findings against open `AbsenceAlert` rows: a key with no open row is new (INSERT
+ a best-effort push); an open row whose key has dropped out of the current
findings is resolved (`resolved_at` set) — the underlying condition cleared,
whether because a round completed, a note was added, or a snag got a status
update. **A finding that persists across ticks keeps the SAME key and is
never re-inserted; a routine's *next* missed window carries a new `since` and
is therefore a new key** — exactly the "exactly one alert, once" contract
this chunk is for.

**Why this doesn't flow through `system_alerts` / the notifications sweep**,
though the design brief for this chunk asked for both. `tasks` already
depends on `notify.push` (provided by `notifications`), which depends on
`system.alerts` (provided by `system`) — see `manifest.py`'s note. Either
`system` or `notifications` taking a capability dependency on anything
`tasks` provides would close a cycle
(`system -> tasks -> notifications -> system`, or
`notifications -> tasks -> notifications`) that
`app/plugin/validate.py::_check_dependency_graph` rejects at boot — the
identical shape the `system` manifest already documents for why it can't
depend on `inbox.query`. So absence alerts are pushed directly, at the
moment a new `AbsenceAlert` row is created, via the SAME `notify.push`
capability `tools.py::_notify` already uses for nudges/transfers (best-effort,
never raises) — and are surfaced for reading via the `tasks_absence_alerts`
MCP tool (tools.py) rather than as a `system_alerts` axis. That also means
the sweep's persistence-gate/re-fire-cooldown/quiet-hours machinery
(`notifications/sweep.py`) does not apply here; it isn't needed for this
shape of alert (there's no flapping condition to dampen — a finding's own
dedup key already guarantees a single send per distinct absence), but it does
mean an absence push is not held during quiet hours the way an infra alert
is. Worth revisiting if that turns out to matter in practice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.integrations.tasks import loops
from app.integrations.tasks.models import (
    AbsenceAlert, Routine, Task, TaskComment, TaskEvent,
)
from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

OPEN_TASK_STATUSES = loops.OPEN_TASK_STATUSES


@dataclass(frozen=True)
class Finding:
    """One distinct "nothing happened" observation.

    `since` anchors the dedup key (see the module docstring) — the timestamp
    of the underlying event (a skip, a due date, a snag's report time), never
    "now": using "now" would mint a fresh key, and therefore a fresh alert,
    on every single tick.
    """

    kind: str
    ref: str
    owner_id: int | None
    since: datetime
    detail: str


def _dedup_key(f: Finding) -> str:
    return f"absence:{f.kind}:{f.ref}:{f.since.isoformat()}"


# ─── findings ────────────────────────────────────────────────────────────


def missed_routine_windows(session: Session, now: datetime) -> list[Finding]:
    """Active routines whose most recent close was a SKIP, not a completion.

    Mirrors `routines._routine_row`'s own "last close" query (field IN
    (NULL, 'skip'), NOT `field.in_((None, "skip"))` — SQL's `x IN (NULL, ...)`
    evaluates to NULL, never true, for a NULL-field row, so `.in_()` would
    silently drop every ordinary completion here too). If that latest close
    event was a skip, the routine missed a window and nobody completed the
    round in time — a finding, keyed on the skip event's own timestamp so the
    *next* miss (a fresh skip) is a new finding, not a resend of this one.
    Resolves itself the moment a later close event (a real completion)
    supersedes the skip — nothing to do here for that; the absent key simply
    stops appearing in this list.
    """
    findings: list[Finding] = []
    routines = session.query(Routine).filter(Routine.active.is_(True)).all()
    for routine in routines:
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
        if last_close is None or last_close.field != "skip":
            continue
        findings.append(Finding(
            kind="routine_window",
            ref=routine.uid,
            owner_id=routine.default_owner_id,
            since=last_close.at,
            detail=(
                f"{routine.title} ({routine.uid}): window closed with no "
                f"round completed"
            ),
        ))
    return findings


def stale_waiting(session: Session, now: datetime, grace: timedelta) -> list[Finding]:
    """`waiting` tasks past their due date, with no note added since, and
    past the configured grace period on top of that.

    "No note since due" rather than "no note ever" — a task can carry notes
    from before it went into `waiting`; what matters is whether anyone has
    followed up *since* it became overdue. Resolves the moment either a note
    lands after `due_at` or the task leaves `waiting` (completed, transferred
    into someone else's queue, whatever) — both simply drop it from this
    query, same self-resolving shape as the routine check above.
    """
    cutoff = now - grace
    findings: list[Finding] = []
    candidates = (
        session.query(Task)
        .filter(
            Task.status == "waiting", Task.due_at.isnot(None), Task.due_at <= cutoff,
            # lios#224: an unconfirmed (LLM-suggested) task is not yet a
            # real "waiting on someone" commitment to alert on.
            Task.confirmed_at.isnot(None),
        )
        .all()
    )
    for t in candidates:
        has_followup = (
            session.query(TaskComment)
            .filter(TaskComment.task_id == t.id, TaskComment.created_at >= t.due_at)
            .first()
            is not None
        )
        if has_followup:
            continue
        findings.append(Finding(
            kind="waiting",
            ref=t.uid,
            owner_id=t.owner_id,
            since=t.due_at,
            detail=f"{t.title} ({t.uid}): waiting, past due, no note since",
        ))
    return findings


def unanswered_snags(session: Session, now: datetime, weeks: int) -> list[Finding]:
    """Snags stuck `open`/`reported` for `weeks` with no trade response,
    read through `snags`' facade (never `snags.models` directly — see
    manifest.py). Household-shared: `owner_id=None`, same as `Snag` itself
    carrying no owner column. Resolves the moment the snag's status moves
    past open/reported (accepted, disputed, fixed, whatever) — again, simply
    drops out of the facade's query.
    """
    cutoff = now - timedelta(weeks=weeks)
    rows = get_capability("snags.query").unanswered(session, cutoff=cutoff)
    return [
        Finding(
            kind="snag",
            ref=row["uid"],
            owner_id=None,
            since=row["origin"],
            detail=(
                f"{row['title']} ({row['uid']}, {row['room']}): "
                f"unanswered ({row['status']}) for {weeks}+ weeks"
            ),
        )
        for row in rows
    ]


def _thresholds() -> tuple[timedelta, int]:
    cfg = plugin_config("tasks")
    grace_days = cfg.absence_waiting_grace_days
    if not isinstance(grace_days, int) or grace_days < 0:
        grace_days = 3
    weeks = cfg.absence_snag_unanswered_weeks
    if not isinstance(weeks, int) or weeks < 0:
        weeks = 3
    return timedelta(days=grace_days), weeks


def all_findings(session: Session, now: datetime) -> list[Finding]:
    """Every current finding, across all three checks. One axis failing must
    never hide the other two — each is wrapped so a `snags.query` hiccup
    (say) doesn't silently swallow routine/waiting findings too."""
    grace, weeks = _thresholds()
    out: list[Finding] = []
    for label, fn in (
        ("routine_window", lambda: missed_routine_windows(session, now)),
        ("waiting", lambda: stale_waiting(session, now, grace)),
        ("snag", lambda: unanswered_snags(session, now, weeks)),
    ):
        try:
            out.extend(fn())
        except Exception:  # noqa: BLE001
            logger.exception("absence: %s check failed", label)
    return out


# ─── persistence + push ─────────────────────────────────────────────────


def _notify_new(finding: Finding) -> None:
    """Best-effort push for a NEWLY created alert — never on a repeat sighting
    of the same dedup key, which is what makes this "exactly once" rather
    than resent every tick like the notifications sweep's own gated items.
    Never raises: a dropped push must never fail the reconcile that found it.
    """
    try:
        get_capability("notify.push").send(
            f"lios: {finding.kind} needs attention",
            finding.detail,
            "warning",
            user_id=finding.owner_id,
            source="tasks",
        )
    except Exception:  # noqa: BLE001 - notify.push may not be registered/configured
        logger.debug("absence: notify.push unavailable", exc_info=True)


def _notify_burst(findings: list[Finding]) -> None:
    """One combined push for a whole batch of new findings, in place of one
    push each (see `_burst_threshold`'s docstring — this is the "digest
    instead of N pushes" side of that guard). Household-wide: a batch this
    size is, by construction, not about one person's queue.
    """
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    breakdown = ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))
    try:
        get_capability("notify.push").send(
            f"lios: {len(findings)} absence findings need attention",
            f"{breakdown}. See tasks_absence_alerts for the full list.",
            "warning",
            source="tasks",
        )
    except Exception:  # noqa: BLE001 - notify.push may not be registered/configured
        logger.debug("absence: notify.push unavailable", exc_info=True)


def _burst_threshold() -> int:
    cfg = plugin_config("tasks")
    value = cfg.absence_burst_threshold
    if not isinstance(value, int) or value < 1:
        return 5
    return value


def reconcile_alerts(session: Session, now: datetime) -> dict:
    """Diff current findings against open `AbsenceAlert` rows and persist the
    delta — the create/resolve half of "exactly one alert, once" (see the
    module docstring). Commits.

    **Burst guard.** Findings are always persisted one row each, regardless
    of count — this only changes how many of them push. Below
    `absence_burst_threshold` new findings in one pass, each still pushes
    individually and immediately (the normal, steady-trickle case this
    module is designed for). At or above it, none of them push individually;
    one combined digest push goes out instead. This is a backfill guard, not
    an ongoing-noise fix — the mechanism was already exactly-once-per-finding
    before this, which is the right shape for a steady trickle of genuinely
    new absences. What it lacked was protection against a whole pre-existing
    backlog crossing the threshold at once (a newly shipped check, or a
    lowered `absence_snag_unanswered_weeks`), which reads identically to a
    real burst of new problems until you count it.
    """
    findings = all_findings(session, now)
    by_key = {_dedup_key(f): f for f in findings}

    open_rows = {
        row.dedup_key: row
        for row in session.query(AbsenceAlert).filter(AbsenceAlert.resolved_at.is_(None)).all()
    }

    created: list[str] = []
    new_findings: list[Finding] = []
    for key, finding in by_key.items():
        if key in open_rows:
            continue
        row = AbsenceAlert(
            dedup_key=key,
            kind=finding.kind,
            ref=finding.ref,
            owner_id=finding.owner_id,
            since=finding.since,
            detail=finding.detail,
        )
        session.add(row)
        created.append(key)
        new_findings.append(finding)

    if new_findings:
        if len(new_findings) >= _burst_threshold():
            _notify_burst(new_findings)
        else:
            for finding in new_findings:
                _notify_new(finding)

    resolved: list[str] = []
    for key, row in open_rows.items():
        if key in by_key:
            continue
        row.resolved_at = now
        resolved.append(key)

    session.commit()
    return {"created": created, "resolved": resolved, "open": len(by_key)}


def open_alerts_for(session: Session, *, owner_id: int | None) -> list[dict]:
    """Open `AbsenceAlert` rows visible to `owner_id` — that user's own
    (`owner_id` matches) plus every household-shared one (`owner_id IS
    NULL`), same "mine plus unowned" shape `NotificationSend`'s reads use.
    `owner_id=None` (household/no-scope caller) sees every open row.
    """
    q = session.query(AbsenceAlert).filter(AbsenceAlert.resolved_at.is_(None))
    if owner_id is not None:
        q = q.filter(or_(AbsenceAlert.owner_id == owner_id, AbsenceAlert.owner_id.is_(None)))
    rows = q.order_by(AbsenceAlert.since).all()
    return [
        {
            "kind": r.kind,
            "ref": r.ref,
            "owner_id": r.owner_id,
            "since": r.since.isoformat(),
            "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
