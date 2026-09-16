"""Writer for the `runs` ledger (S5.1) — see `app/models/runs.py` for the
table's design rationale, including the Wave 5.1 merge with `tool_calls`.

`record_run` is a context manager: it inserts a row on enter and updates it
on exit, using its OWN short-lived session (`get_db().session()`), entirely
independent of whatever session the wrapped job opens for its own work. That
is deliberate, not incidental: a job that raises and rolls back its own
transaction must not also lose the fact that it ran and failed — the run
record is the audit trail *of* the failure, so it cannot live inside the
same transaction the failure rolls back. Two separate connections/sessions
is what makes that true; sharing one would mean a job's own `session.
rollback()` could silently take the run row down with it.

Usage:

    with record_run("scheduled_job", "sync_google_calendar", trigger="schedule") as run:
        do_the_sync()
        run.touched(events_synced=42)

On a raised exception, `outcome` is set to `error` and `error_text` captures
`str(exc)` (truncated); the exception is always re-raised — this context
manager observes, it never swallows. Never raises itself on the *ledger*
side: a `get_db()` failure or similar is caught and logged, exactly like
`record_tool_call` below — the ledger must never be why a job (or a tool
call) fails.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Lets a job called *underneath* a `record_run` block reach the handle
# without it being threaded through every call site as an argument — this
# is what `app.scheduler`'s generic wrapper (which owns the `with` block)
# and a job's own body (which knows what it touched) need to share. Safe
# across `asyncio.to_thread`: it runs the callable via
# `contextvars.copy_context().run(...)`, so a value set here before the
# `to_thread` call is visible inside it (verified — this is not incidental).
_current_run: ContextVar["RunHandle | None"] = ContextVar("_current_run", default=None)


def current_run() -> "RunHandle | None":
    """The `RunHandle` for the innermost enclosing `record_run` block, or
    `None` outside one (e.g. called directly from a test, or a manual
    invocation with no ledger wrapper). Callers must handle `None`."""
    return _current_run.get()

# Same cap as tool_calls.record_tool_call — one verbose traceback-derived
# message must not bloat the ledger.
_MAX_ERROR_LEN = 2000


@dataclass
class RunHandle:
    """Yielded by `record_run`. `touched(**counts)` records what the job did;
    called any number of times (or zero) inside the `with` block — later
    calls merge into the same dict rather than replacing it, so a job that
    reports counts in more than one place doesn't clobber its own first call.
    """

    _touched: dict[str, Any] = field(default_factory=dict)
    _skip_reason: str | None = field(default=None)

    def touched(self, **counts: Any) -> None:
        self._touched.update(counts)

    def skip(self, reason: str | None = None) -> None:
        """Mark the run `skipped` rather than `ok` (e.g. nothing was due).
        Only takes effect if the block exits without raising — an exception
        after calling this still records `error`, since that's what actually
        happened."""
        self._skip_reason = reason or "skipped"


def _insert_started(
    *, run_id: str, kind: str, name: str, user_id: int | None,
    started_at: datetime, trigger: str,
) -> None:
    from app.db import get_db
    from app.models.runs import Run

    try:
        db = get_db()
        with db.session() as session:
            session.add(Run(
                run_id=run_id,
                kind=kind,
                name=name,
                user_id=user_id,
                started_at=started_at,
                # Placeholder until _finish overwrites it — "error" rather
                # than "ok" so a process that dies between insert and
                # __exit__ (killed, OOM) leaves a row that reads as failed,
                # never a silent false "ok".
                outcome="error",
                trigger=trigger,
            ))
            session.commit()
    except Exception:
        logger.warning("Failed to insert runs row for %s/%s (run_id=%s)", kind, name, run_id, exc_info=True)


def _finish(
    *, run_id: str, outcome: str, error_text: str | None,
    finished_at: datetime, duration_ms: int, touched: dict[str, Any] | None,
) -> None:
    from app.db import get_db
    from app.models.runs import Run

    try:
        db = get_db()
        with db.session() as session:
            row = session.query(Run).filter(Run.run_id == run_id).one_or_none()
            if row is None:
                # The insert itself failed (already logged there) — nothing
                # to update. Never raise from here either.
                return
            row.outcome = outcome
            row.error_text = error_text[:_MAX_ERROR_LEN] if error_text else None
            row.finished_at = finished_at
            row.duration_ms = duration_ms
            row.touched = touched or None
            session.commit()
    except Exception:
        logger.warning("Failed to finalize runs row (run_id=%s)", run_id, exc_info=True)


@contextmanager
def record_run(
    kind: str,
    name: str,
    *,
    user_id: int | None = None,
    trigger: str = "schedule",
) -> Iterator[RunHandle]:
    """Insert a `runs` row on enter, update it on exit. See module docstring.

    `kind` is `scheduled_job` | `tool_call` | `manual` | `script` (the last
    added Wave 5.2 for hand-run maintenance scripts like
    `app/scripts/reembed.py`, whose in-progress state `system_alerts`' index
    axis reads directly — see `app.integrations.system.tools.
    _backfill_run_in_progress`). `trigger` is
    `schedule` | `mcp` | `rest` | `cli`.
    """
    run_id = secrets.token_hex(4)
    started_at = datetime.now(timezone.utc)
    start = time.monotonic()

    _insert_started(
        run_id=run_id, kind=kind, name=name, user_id=user_id,
        started_at=started_at, trigger=trigger,
    )

    handle = RunHandle()
    token = _current_run.set(handle)
    outcome = "ok"
    error_text: str | None = None
    try:
        yield handle
        if handle._skip_reason is not None:
            outcome = "skipped"
            handle._touched.setdefault("skip_reason", handle._skip_reason)
    except Exception as exc:
        outcome = "error"
        error_text = str(exc)
        raise
    finally:
        _current_run.reset(token)
        duration_ms = int((time.monotonic() - start) * 1000)
        _finish(
            run_id=run_id, outcome=outcome, error_text=error_text,
            finished_at=datetime.now(timezone.utc), duration_ms=duration_ms,
            touched=handle._touched,
        )


# ---------------------------------------------------------------------------
# Tool-call writer (Wave 5.1 — ex-`app.services.tool_calls.record_tool_call`).
# ---------------------------------------------------------------------------


def record_tool_call(
    *,
    name: str,
    user_id: int | None,
    duration_ms: int,
    status: str,
    error: str | None,
    tool_call_id: str,
    args_summary: str | None = None,
    affected: list[str] | None = None,
    source_ip: str | None = None,
    transport: str | None = None,
) -> None:
    """Insert one `runs` row for a completed tool dispatch (`kind="tool_call"`).

    Same call signature as the pre-merge `app.services.tool_calls.
    record_tool_call` it replaces — `app.plugin.dispatch` calls this
    unchanged apart from its import path. Unlike `record_run` (a two-phase
    context manager wrapping a job that hasn't finished yet), this writes a
    single complete row: `dispatch_tool` already knows the final outcome,
    duration and (redacted) arguments by the time it calls in, so there is
    no "insert placeholder, update on exit" phase to do.

    `run_id` is `tool_call_id` verbatim — the correlation id already in the
    completion log line and `ToolResult.tool_call_id`, reused rather than
    minting a second one. `user_id` is the caller (NOT nullable-household
    like a scheduled job's `user_id`) — this is what makes a `tool_call` row
    caller-scoped in `recent_activity` below. Never raises — a persistence
    failure here must never break tool dispatch, exactly like every other
    writer in this module and its pre-merge predecessor.
    """
    from app.db import get_db
    from app.models.runs import Run

    try:
        db = get_db()
        with db.session() as session:
            now = datetime.now(timezone.utc)
            session.add(Run(
                run_id=tool_call_id,
                kind="tool_call",
                name=name,
                user_id=user_id,
                started_at=now,
                finished_at=now,
                duration_ms=duration_ms,
                outcome=status,
                error_text=error[:_MAX_ERROR_LEN] if error else None,
                touched=None,
                # The column is 10 chars ("mcp"|"http"). A longer label from
                # a script or a new transport must shorten, not lose the row
                # — on 2026-09-02 a 21-char label dropped the audit for ~60
                # dispatches while the calls themselves succeeded (pre-merge
                # incident; same column, same cap, carried over verbatim).
                trigger=transport[:10] if transport else "mcp",
                args_summary=args_summary,
                affected=json.dumps(affected) if affected else None,
                source_ip=source_ip,
            ))
            session.commit()
    except Exception:
        logger.warning(
            "Failed to record tool-call runs row for %s (tool_call_id=%s)",
            name, tool_call_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Read side. `recent_activity` is the raw filterable view — used directly by
# the `system_runs` MCP tool, and by `recent_activity_summary` below as its
# data source, so the two can never quietly diverge on what rows count as
# "recent activity" (window, scoping). `recent_activity_summary` is a
# separate shape (Wave 5.11) for `system_alerts`' `recent_runs` axis, which
# needs a bounded, at-a-glance payload rather than every matching row.
# ---------------------------------------------------------------------------


def recent_activity(
    session,
    *,
    since: datetime,
    scope_user_id: int | None,
    name: str | None = None,
    outcome: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """View over the `runs` ledger (S5.1; absorbed `tool_calls` Wave 5.1)
    for everything at or after `since`.

    Scoping (see `app/models/runs.py`'s module docstring for the full
    rationale): `scheduled_job`/`manual` rows are household-wide and are
    NEVER filtered by `scope_user_id` — a per-user caller still sees every
    scheduled job. `tool_call` rows DO carry a real per-call `user_id`, so a
    scoped caller (`scope_user_id` not None) sees only their own tool calls;
    `scope_user_id=None` (the household/dashboard view) sees every call.
    Expressed as one query (`kind != 'tool_call' OR user_id == scope_user_id`)
    rather than two separately-scoped queries, so the split can't drift.

    `outcome` filters the one vocabulary the column actually holds —
    ok/error/skipped for scheduled_job/manual rows, ok/error/timeout for
    tool_call rows — so e.g. `outcome="skipped"` only ever matches
    scheduled_job/manual rows: no tool call can be "skipped".

    Returns `{"items": [...], "counts": {...}}`, items newest-first, each
    `{name, kind, started_at, duration_ms, outcome, trigger}`.
    """
    from sqlalchemy import or_

    from app.models.runs import Run

    q = session.query(Run).filter(Run.started_at >= since)
    if scope_user_id is not None:
        q = q.filter(or_(Run.kind != "tool_call", Run.user_id == scope_user_id))
    if name:
        q = q.filter(Run.name.ilike(f"%{name}%"))
    if outcome:
        q = q.filter(Run.outcome == outcome)
    rows = q.order_by(Run.started_at.desc()).limit(limit).all()

    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for r in rows:
        items.append({
            "name": r.name,
            "kind": r.kind,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "duration_ms": r.duration_ms,
            "outcome": r.outcome,
            "trigger": r.trigger,
        })
        counts[r.outcome] = counts.get(r.outcome, 0) + 1

    items.sort(key=lambda x: x["started_at"] or "", reverse=True)
    return {"items": items[:limit], "counts": counts}


# Safety valve for `recent_activity_summary`'s aggregation query below. The
# normal `recent_runs` window (60 min) sees at most a few hundred rows; this
# only guards a caller widening `since` a long way out from also widening the
# in-memory aggregation without bound. Not the mechanism that fixed the
# 147-row flood (that was mostly one name's volume, not row count against a
# limit) — see the module-level rationale in the function docstring.
_SUMMARY_QUERY_CAP = 5000

# Ranks outcomes worst-first so a name's `worst_outcome` reflects the worst
# thing it did in the window, not merely its most recent run.
_OUTCOME_RANK = {"ok": 0, "skipped": 1, "timeout": 2, "error": 3}


def recent_activity_summary(
    session,
    *,
    since: datetime,
    scope_user_id: int | None,
    items_limit: int = 20,
) -> dict[str, Any]:
    """By-name summary for `system_alerts`' `recent_runs` axis (Wave 5.11).

    Replaces returning every matching row verbatim. Measured on the live
    system 2026-09-05: a 60-minute window held 147 rows, and
    `whatsapp_bridge_heartbeat` (a scheduled job every minute) was most of
    them — 1,440 identical `ok` rows a day from one job in a ledger meant to
    answer "what ran and what happened". A flat row dump scales with call
    volume, not with anything a reader needs to know.

    Same scoping as `recent_activity` (see that function and
    `app/models/runs.py`'s module docstring for the full rationale):
    `scheduled_job`/`manual` rows are household-wide and never filtered by
    `scope_user_id`; `tool_call` rows are scoped to the caller when one is
    bound.

    Returns:
      - `by_name`: one entry per `(name, kind)` seen in the window —
        `{name, kind, count, last_started_at, worst_outcome,
        max_duration_ms, avg_duration_ms}` — sorted so any name with a
        non-`ok` `worst_outcome` sorts before every all-`ok` name (ties
        broken by run count, descending, then name). This is what makes a
        noisy but healthy job (a heartbeat, a frequent sync) sink below
        anything that actually needs attention, without dropping it.
      - `counts`: totals by outcome across the whole window (unchanged
        from `recent_activity` — still cheap, still useful as a glance
        figure).
      - `items`: only the non-`ok` runs, newest first, capped at
        `items_limit` (default 20) — the detail a summary can't carry.
        `items_truncated` is True when more non-ok runs existed than fit.
    """
    from sqlalchemy import or_

    from app.models.runs import Run

    q = session.query(Run).filter(Run.started_at >= since)
    if scope_user_id is not None:
        q = q.filter(or_(Run.kind != "tool_call", Run.user_id == scope_user_id))
    rows = q.order_by(Run.started_at.desc()).limit(_SUMMARY_QUERY_CAP).all()

    counts: dict[str, int] = {}
    by_name: dict[tuple[str, str], dict[str, Any]] = {}
    non_ok_items: list[dict[str, Any]] = []

    for r in rows:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
        started = r.started_at.isoformat() if r.started_at else None

        key = (r.name, r.kind)
        entry = by_name.get(key)
        if entry is None:
            entry = {
                "name": r.name,
                "kind": r.kind,
                "count": 0,
                "last_started_at": None,
                "worst_outcome": "ok",
                "max_duration_ms": None,
                "_duration_sum": 0,
                "_duration_n": 0,
            }
            by_name[key] = entry
        entry["count"] += 1
        if entry["last_started_at"] is None or (started or "") > (entry["last_started_at"] or ""):
            entry["last_started_at"] = started
        if _OUTCOME_RANK.get(r.outcome, 0) > _OUTCOME_RANK.get(entry["worst_outcome"], 0):
            entry["worst_outcome"] = r.outcome
        if r.duration_ms is not None:
            entry["_duration_sum"] += r.duration_ms
            entry["_duration_n"] += 1
            if entry["max_duration_ms"] is None or r.duration_ms > entry["max_duration_ms"]:
                entry["max_duration_ms"] = r.duration_ms

        if r.outcome != "ok":
            non_ok_items.append({
                "name": r.name,
                "kind": r.kind,
                "started_at": started,
                "duration_ms": r.duration_ms,
                "outcome": r.outcome,
                "trigger": r.trigger,
            })

    by_name_list: list[dict[str, Any]] = []
    for entry in by_name.values():
        n = entry.pop("_duration_n")
        total = entry.pop("_duration_sum")
        entry["avg_duration_ms"] = int(total / n) if n else None
        by_name_list.append(entry)

    by_name_list.sort(key=lambda e: (e["worst_outcome"] == "ok", -e["count"], e["name"]))
    non_ok_items.sort(key=lambda x: x["started_at"] or "", reverse=True)

    return {
        "by_name": by_name_list,
        "counts": counts,
        "items": non_ok_items[:items_limit],
        "items_truncated": len(non_ok_items) > items_limit,
    }
