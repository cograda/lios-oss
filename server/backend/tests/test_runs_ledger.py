"""The runs ledger (db tier) — S5.1, `vault/Projects/lios/Backlog.md`.
Wave 5.1 (2026-09-05): `tool_calls` absorbed into `runs` as `kind="tool_call"`
rows — this file's scoping tests now seed those directly instead of a
separate `ToolCall` row. Wave 5.11 (2026-09-05): `recent_runs` bounded and
summarised (`recent_activity_summary`) — `items` now holds only non-ok runs;
`by_name` is the summary every seeded (ok) name shows up in.

Covers:
  - `record_run` writes ok/error rows, capturing the exception message on
    error, and its own-session write survives even when the caller's own
    session/transaction is rolled back (`real_db_concurrent` — genuine
    independent connections, not `real_db`'s shared-connection SAVEPOINT).
  - `app.scheduler._wrap_scheduled_job` — the generic wrap every registered
    job goes through — writes one row per execution with zero per-job code
    (and, with `ledger=False`, writes none at all).
  - `system_alerts`' `recent_runs` axis: a seeded run inside the window
    appears in `by_name`, one outside it does not; the `by_name` summary
    shape, non-ok-first ordering, and the `items` cap.
  - `recent_runs` (and the `system_runs` tool built on the same
    `recent_activity` helper) scope `kind="tool_call"` rows to the caller
    but never filter `scheduled_job`/`manual` rows — scheduled jobs are
    household-wide.
  - Kernel prune jobs: tool_call rows at 30 days, scheduled_job/manual rows
    at 90 days.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed_run(session, *, run_id, name, started_at, outcome="ok", kind="scheduled_job",
              duration_ms=100, trigger="schedule", user_id=None):
    from app.models.runs import Run

    session.add(Run(
        run_id=run_id, kind=kind, name=name, user_id=user_id,
        started_at=started_at, finished_at=started_at + timedelta(milliseconds=duration_ms),
        duration_ms=duration_ms, outcome=outcome, trigger=trigger,
    ))


def _seed_tool_call(session, *, name, user_id, called_at, status="ok"):
    """A `kind="tool_call"` run row — the post-merge shape of what used to
    be a standalone `ToolCall` row."""
    _seed_run(
        session, run_id="seed" + name[:4].ljust(4, "0"), name=name, user_id=user_id,
        started_at=called_at, outcome=status, kind="tool_call", duration_ms=10, trigger="mcp",
    )


# ---------------------------------------------------------------------------
# record_run
# ---------------------------------------------------------------------------


def test_record_run_writes_ok_row(db_session):
    from app.models.runs import Run
    from app.services.runs import record_run

    with record_run("manual", "probe_ok", trigger="cli") as run:
        run.touched(widgets=3)

    row = db_session.query(Run).filter_by(name="probe_ok").one()
    assert row.kind == "manual"
    assert row.trigger == "cli"
    assert row.outcome == "ok"
    assert row.error_text is None
    assert row.finished_at is not None
    assert row.duration_ms is not None
    assert row.touched == {"widgets": 3}


def test_record_run_writes_error_row_and_reraises(db_session):
    from app.models.runs import Run
    from app.services.runs import record_run

    with pytest.raises(RuntimeError, match="boom"):
        with record_run("manual", "probe_error", trigger="cli"):
            raise RuntimeError("boom")

    row = db_session.query(Run).filter_by(name="probe_error").one()
    assert row.outcome == "error"
    assert "boom" in row.error_text


def test_record_run_skip(db_session):
    from app.models.runs import Run
    from app.services.runs import record_run

    with record_run("manual", "probe_skip", trigger="cli") as run:
        run.skip("nothing due")

    row = db_session.query(Run).filter_by(name="probe_skip").one()
    assert row.outcome == "skipped"
    assert row.touched == {"skip_reason": "nothing due"}


def test_record_run_survives_callers_own_session_rollback(real_db_concurrent):
    """The whole point of record_run's own session (module docstring):
    a job that opens its own session/transaction and rolls it back after
    failing must not take the run record down with it. Needs genuinely
    independent connections (`real_db_concurrent`) — `real_db`'s shared
    SAVEPOINT connection wouldn't distinguish this from the buggy version.
    """
    from sqlalchemy import text

    from app.db import get_db
    from app.models.runs import Run
    from app.services.runs import record_run

    db = get_db()
    callers_own_session = db.SessionLocal()
    try:
        with pytest.raises(RuntimeError, match="job failed"):
            with record_run("manual", "probe_isolation", trigger="cli"):
                # The wrapped job's own work, on its OWN session — then it
                # fails and rolls that transaction back.
                callers_own_session.execute(text("SELECT 1"))
                callers_own_session.rollback()
                raise RuntimeError("job failed")
    finally:
        callers_own_session.close()

    with db.session() as verify_session:
        row = verify_session.query(Run).filter_by(name="probe_isolation").one()
        assert row.outcome == "error"
        assert "job failed" in row.error_text


# ---------------------------------------------------------------------------
# Scheduler wrap
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_scheduler_wrap_writes_one_row_per_execution(db_session):
    from app.models.runs import Run
    from app.scheduler import _wrap_scheduled_job

    calls = []

    async def fake_job(x):
        calls.append(x)

    wrapped = _wrap_scheduled_job("probe_scheduled", fake_job)
    await wrapped("hello")
    await wrapped("hello")

    assert calls == ["hello", "hello"]
    rows = db_session.query(Run).filter_by(name="probe_scheduled").all()
    assert len(rows) == 2
    assert all(r.kind == "scheduled_job" and r.trigger == "schedule" for r in rows)
    assert all(r.outcome == "ok" for r in rows)


@pytest.mark.anyio
async def test_scheduler_wrap_records_error_outcome(db_session):
    from app.models.runs import Run
    from app.scheduler import _wrap_scheduled_job

    async def failing_job():
        raise ValueError("kaboom")

    wrapped = _wrap_scheduled_job("probe_scheduled_fail", failing_job)
    with pytest.raises(ValueError):
        await wrapped()

    row = db_session.query(Run).filter_by(name="probe_scheduled_fail").one()
    assert row.outcome == "error"
    assert "kaboom" in row.error_text


# ---------------------------------------------------------------------------
# system_alerts `recent_runs` axis
# ---------------------------------------------------------------------------


def test_recent_runs_shows_run_within_window_not_outside(db_session):
    from app.integrations.system.tools import handle_alerts_household

    now = datetime.now(timezone.utc)
    _seed_run(db_session, run_id="aaaa1111", name="in_window_job", started_at=now - timedelta(minutes=5))
    _seed_run(db_session, run_id="bbbb2222", name="out_of_window_job", started_at=now - timedelta(hours=3))
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    names = {entry["name"] for entry in payload["recent_runs"]["by_name"]}
    assert "in_window_job" in names
    assert "out_of_window_job" not in names
    assert payload["recent_runs"]["counts"].get("ok", 0) >= 1
    # Both seeded rows are ok, so neither appears in the non-ok-only items cap.
    assert payload["recent_runs"]["items"] == []


def test_recent_runs_window_is_configurable(db_session):
    from app.integrations.system.tools import handle_alerts_household

    now = datetime.now(timezone.utc)
    _seed_run(db_session, run_id="cccc3333", name="two_hours_ago_job", started_at=now - timedelta(hours=2))
    db_session.commit()

    default_payload = json.loads(handle_alerts_household(db_session, {}))
    names_default = {i["name"] for i in default_payload["recent_runs"]["by_name"]}
    assert "two_hours_ago_job" not in names_default

    widened_payload = json.loads(
        handle_alerts_household(db_session, {"recent_runs_minutes": 180})
    )
    names_widened = {i["name"] for i in widened_payload["recent_runs"]["by_name"]}
    assert "two_hours_ago_job" in names_widened


def test_recent_runs_by_name_summary_shape_and_ordering(db_session):
    """`by_name` aggregates per (name, kind): count, last_started_at,
    worst_outcome, max/avg duration_ms — and sorts any non-ok name before
    every all-ok name, regardless of run count."""
    from app.integrations.system.tools import handle_alerts_household

    now = datetime.now(timezone.utc)
    # A noisy but healthy job — three ok runs, like a per-minute heartbeat.
    for i, mins in enumerate((3, 2, 1)):
        _seed_run(
            db_session, run_id=f"noisy{i:03d}0", name="noisy_ok_job",
            started_at=now - timedelta(minutes=mins), duration_ms=100 + i,
        )
    # A quiet job with a single failure.
    _seed_run(
        db_session, run_id="quietfail0", name="quiet_failing_job",
        started_at=now - timedelta(minutes=1), outcome="error", duration_ms=50,
    )
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    by_name = {entry["name"]: entry for entry in payload["recent_runs"]["by_name"]}

    noisy = by_name["noisy_ok_job"]
    assert noisy["count"] == 3
    assert noisy["worst_outcome"] == "ok"
    assert noisy["max_duration_ms"] == 102
    assert noisy["avg_duration_ms"] == 101
    assert noisy["last_started_at"] is not None

    failing = by_name["quiet_failing_job"]
    assert failing["count"] == 1
    assert failing["worst_outcome"] == "error"

    # Non-ok names sort before all-ok names regardless of volume.
    names_in_order = [entry["name"] for entry in payload["recent_runs"]["by_name"]]
    assert names_in_order.index("quiet_failing_job") < names_in_order.index("noisy_ok_job")

    # And the failure (only) shows up in the capped items list.
    item_names = {item["name"] for item in payload["recent_runs"]["items"]}
    assert item_names == {"quiet_failing_job"}


def test_recent_runs_items_cap_is_configurable(db_session):
    from app.integrations.system.tools import handle_alerts_household

    now = datetime.now(timezone.utc)
    for i in range(5):
        _seed_run(
            db_session, run_id=f"fail{i:04d}", name=f"failing_job_{i}",
            started_at=now - timedelta(minutes=1), outcome="error",
        )
    db_session.commit()

    default_payload = json.loads(handle_alerts_household(db_session, {}))
    assert len(default_payload["recent_runs"]["items"]) == 5
    assert default_payload["recent_runs"]["items_truncated"] is False

    capped_payload = json.loads(
        handle_alerts_household(db_session, {"recent_runs_items_limit": 2})
    )
    assert len(capped_payload["recent_runs"]["items"]) == 2
    assert capped_payload["recent_runs"]["items_truncated"] is True
    # But by_name still reports every failing job — the summary is never capped.
    assert len(capped_payload["recent_runs"]["by_name"]) == 5


# ---------------------------------------------------------------------------
# Scoping: runs are household-wide, tool_calls are per-caller
# ---------------------------------------------------------------------------


def test_recent_runs_scopes_tool_calls_but_not_scheduled_jobs(db_session):
    from app.auth.context import use_user
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    _seed_run(db_session, run_id="dddd4444", name="household_job", started_at=now - timedelta(minutes=2))
    _seed_tool_call(db_session, name="alex_tool", user_id=1, called_at=now - timedelta(minutes=2))
    _seed_tool_call(db_session, name="sam_tool", user_id=2, called_at=now - timedelta(minutes=2))
    db_session.commit()

    with use_user(1):
        payload = json.loads(handle_alerts(db_session, {}))

    names = {entry["name"] for entry in payload["recent_runs"]["by_name"]}
    assert "household_job" in names, "scheduled jobs must be visible regardless of caller"
    assert "alex_tool" in names, "the caller's own tool calls must be visible"
    assert "sam_tool" not in names, "another user's tool calls must never leak"


def test_recent_runs_household_view_sees_every_tool_call(db_session):
    from app.integrations.system.tools import handle_alerts_household

    now = datetime.now(timezone.utc)
    _seed_tool_call(db_session, name="alex_tool2", user_id=1, called_at=now - timedelta(minutes=2))
    _seed_tool_call(db_session, name="sam_tool2", user_id=2, called_at=now - timedelta(minutes=2))
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    names = {entry["name"] for entry in payload["recent_runs"]["by_name"]}
    assert {"alex_tool2", "sam_tool2"} <= names


# ---------------------------------------------------------------------------
# system_runs MCP tool
# ---------------------------------------------------------------------------


def test_system_runs_filters_by_name(db_session):
    from app.integrations.system.tools import handle_runs

    now = datetime.now(timezone.utc)
    _seed_run(db_session, run_id="eeee5555", name="job_a", started_at=now - timedelta(minutes=10))
    _seed_run(db_session, run_id="eeee6666", name="job_b", started_at=now - timedelta(minutes=10))
    db_session.commit()

    payload = json.loads(handle_runs(db_session, {"name": "job_a"}))
    names = {i["name"] for i in payload["items"]}
    assert names == {"job_a"}


def test_system_runs_filters_by_outcome(db_session):
    from app.integrations.system.tools import handle_runs

    now = datetime.now(timezone.utc)
    _seed_run(db_session, run_id="ffff7777", name="job_ok", started_at=now - timedelta(minutes=10), outcome="ok")
    _seed_run(db_session, run_id="ffff8888", name="job_err", started_at=now - timedelta(minutes=10), outcome="error")
    db_session.commit()

    payload = json.loads(handle_runs(db_session, {"outcome": "error"}))
    names = {i["name"] for i in payload["items"]}
    assert "job_err" in names
    assert "job_ok" not in names


def test_system_runs_respects_since(db_session):
    from app.integrations.system.tools import handle_runs

    now = datetime.now(timezone.utc)
    _seed_run(db_session, run_id="99991111", name="job_old", started_at=now - timedelta(days=3))
    db_session.commit()

    default_payload = json.loads(handle_runs(db_session, {}))
    assert "job_old" not in {i["name"] for i in default_payload["items"]}

    since = (now - timedelta(days=4)).isoformat()
    wide_payload = json.loads(handle_runs(db_session, {"since": since}))
    assert "job_old" in {i["name"] for i in wide_payload["items"]}


def test_system_runs_tool_calls_scoped_to_caller(db_session):
    from app.auth.context import use_user
    from app.integrations.system.tools import handle_runs

    now = datetime.now(timezone.utc)
    _seed_tool_call(db_session, name="alex_only", user_id=1, called_at=now - timedelta(minutes=2))
    _seed_tool_call(db_session, name="sam_only", user_id=2, called_at=now - timedelta(minutes=2))
    db_session.commit()

    with use_user(1):
        payload = json.loads(handle_runs(db_session, {}))
    names = {i["name"] for i in payload["items"]}
    assert "alex_only" in names
    assert "sam_only" not in names


# ---------------------------------------------------------------------------
# Heartbeat-class jobs: ledger=False (Wave 5.11)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_wrap_scheduled_job_ledger_false_writes_no_row(db_session):
    """The whatsapp_bridge_heartbeat decision: a `TaskSpec(ledger=False)`
    job still runs (and its own body's side effects still happen), but
    `_wrap_scheduled_job` skips the `runs` insert entirely — no row at all,
    not even on success."""
    from app.models.runs import Run
    from app.scheduler import _wrap_scheduled_job

    calls = []

    async def fake_heartbeat():
        calls.append(1)

    wrapped = _wrap_scheduled_job("probe_unledgered", fake_heartbeat, ledger=False)
    await wrapped()
    await wrapped()

    assert calls == [1, 1], "the wrapped job itself must still execute"
    rows = db_session.query(Run).filter_by(name="probe_unledgered").all()
    assert rows == []


@pytest.mark.anyio
async def test_wrap_scheduled_job_ledger_default_still_writes(db_session):
    """Mutation check for the above: the default (no `ledger` kwarg) must
    still ledger, or the whole axis 10 test suite above would be silently
    exercising nothing."""
    from app.models.runs import Run
    from app.scheduler import _wrap_scheduled_job

    async def fake_job():
        pass

    wrapped = _wrap_scheduled_job("probe_ledgered_default", fake_job)
    await wrapped()

    row = db_session.query(Run).filter_by(name="probe_ledgered_default").one()
    assert row.outcome == "ok"


def test_whatsapp_bridge_heartbeat_manifest_is_unledgered():
    """Pins the actual decision, not just the mechanism: the manifest entry
    for `whatsapp_bridge_heartbeat` declares `ledger=False`. Bridge-liveness
    detection when the heartbeat stops is unaffected — it's driven by the
    `whatsapp_bridge` `SyncState` row `heartbeat._probe_one` writes every
    run (age/consecutive_failures, system_alerts axis 1), never by the
    `runs` ledger."""
    from app.integrations.whatsapp.manifest import MANIFEST

    tasks = {t.name: t for t in MANIFEST.background_tasks}
    assert tasks["whatsapp_bridge_heartbeat"].ledger is False


# ---------------------------------------------------------------------------
# Kernel prune jobs: tool_call at 30 days, scheduled_job/manual at 90 days
# (Wave 5.11)
# ---------------------------------------------------------------------------


def test_prune_tool_calls_deletes_only_old_tool_call_rows(db_session):
    from app.models.runs import Run
    from app.plugin.kernel_jobs import _prune_tool_calls_blocking

    now = datetime.now(timezone.utc)
    _seed_run(db_session, run_id="oldtc0001", name="old_tool_call", kind="tool_call",
              started_at=now - timedelta(days=31), trigger="mcp", user_id=1)
    _seed_run(db_session, run_id="newtc0001", name="new_tool_call", kind="tool_call",
              started_at=now - timedelta(days=1), trigger="mcp", user_id=1)
    _seed_run(db_session, run_id="oldsj0001", name="old_scheduled_job",
              started_at=now - timedelta(days=31))
    db_session.commit()

    _prune_tool_calls_blocking()
    db_session.expire_all()

    remaining = {r.name for r in db_session.query(Run).all()}
    assert "old_tool_call" not in remaining
    assert "new_tool_call" in remaining
    # scheduled_job rows are this job's business, not the tool_call prune's.
    assert "old_scheduled_job" in remaining


def test_prune_scheduled_runs_deletes_old_scheduled_and_manual_rows(db_session):
    from app.models.runs import Run
    from app.plugin.kernel_jobs import _prune_scheduled_runs_blocking

    now = datetime.now(timezone.utc)
    _seed_run(db_session, run_id="oldsj0002", name="old_scheduled_job",
              started_at=now - timedelta(days=91))
    _seed_run(db_session, run_id="oldmn0001", name="old_manual", kind="manual",
              started_at=now - timedelta(days=91))
    _seed_run(db_session, run_id="newsj0002", name="new_scheduled_job",
              started_at=now - timedelta(days=1))
    _seed_run(db_session, run_id="oldtc0002", name="old_tool_call_kept", kind="tool_call",
              started_at=now - timedelta(days=91), trigger="mcp", user_id=1)
    db_session.commit()

    _prune_scheduled_runs_blocking()
    db_session.expire_all()

    remaining = {r.name for r in db_session.query(Run).all()}
    assert "old_scheduled_job" not in remaining
    assert "old_manual" not in remaining
    assert "new_scheduled_job" in remaining
    # tool_call rows are the other prune job's business, not this one's —
    # even an old one survives this pass.
    assert "old_tool_call_kept" in remaining


def test_prune_scheduled_runs_respects_90_day_cutoff_not_30(db_session):
    """Mutation check surfaced directly: a scheduled_job row at 45 days old
    (older than tool_calls' 30-day window, younger than this job's 90) must
    survive — proves the cutoff is actually 90 days, not an accidental
    30-day copy-paste from `_prune_tool_calls_blocking`."""
    from app.models.runs import Run
    from app.plugin.kernel_jobs import _prune_scheduled_runs_blocking

    now = datetime.now(timezone.utc)
    _seed_run(db_session, run_id="midsj0001", name="45_day_old_job",
              started_at=now - timedelta(days=45))
    db_session.commit()

    _prune_scheduled_runs_blocking()
    db_session.expire_all()

    remaining = {r.name for r in db_session.query(Run).all()}
    assert "45_day_old_job" in remaining
