"""Dashboard-facing system routes (db tier).

`/api/system/alerts` and `/api/system/tool-stats` are thin read-only
wrappers the React dashboard's Alerts panel consumes — added as part of
the Phase 5 follow-up (`system_alerts` + `tool_calls` existed already;
this is just wiring them onto an HTTP route). `tool_calls` was absorbed
into the `runs` ledger as `kind="tool_call"` rows in Wave 5.1 (2026-09-05);
these routes now read `runs` filtered by kind. Covers:

  (a) /system/alerts returns the same shape as the `system_alerts` MCP
      tool (handle_alerts), including a tripped tool_alerts entry.
  (b) /system/tool-stats aggregates calls/errors/p50/p95 per tool over
      the window, orders by error rate then p95, and folds in an
      errored-but-low-volume tool that would otherwise fall outside the
      top-20-by-volume cut.
"""

import secrets
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


def _seed_tool_call(session, *, name, status, duration_ms, called_at, user_id=1):
    from app.models.runs import Run

    session.add(Run(
        run_id=secrets.token_hex(4),
        kind="tool_call",
        name=name,
        user_id=user_id,
        started_at=called_at,
        finished_at=called_at,
        duration_ms=duration_ms,
        outcome=status,
        error_text="seeded failure" if status == "error" else None,
        trigger="mcp",
    ))


@pytest.mark.anyio
async def test_system_alerts_route_matches_handle_alerts_shape(db_session):
    from app.routes.system import system_alerts

    now = datetime.now(timezone.utc)
    for _ in range(4):  # > TOOL_FAILURE_THRESHOLD (3)
        _seed_tool_call(
            db_session, name="flaky_tool", status="error",
            duration_ms=50, called_at=now - timedelta(minutes=5),
        )
    db_session.commit()

    payload = await system_alerts()

    assert payload["status"] == "degraded"
    assert any(
        a["tool"] == "flaky_tool" and "failing repeatedly" in a["issue"]
        for a in payload["tool_alerts"]
    )
    # Same axes as the MCP tool's payload. `restore_drill` and
    # `daemon_status` added R2 (2026-09-05); `index_state` added R4
    # (2026-09-04) — see app/integrations/system/restore_drill.py and
    # `_index_state()` in app/integrations/system/tools.py respectively.
    # `recent_runs` added S5.1 (2026-09-05) — see app/services/runs.py.
    # `device_fleet` added Strand A (2026-09-08) — see
    # app/integrations/system/device_fleet.py. `host_fleet` added the same
    # day (host-liveness alerting, one layer below device_fleet) — see
    # app/integrations/system/host_fleet.py.
    assert set(payload.keys()) == {
        "status", "alerts", "data_freshness", "unmeasured", "reauth_needed", "tool_alerts",
        "restore_drill", "daemon_status", "index_state", "recent_runs", "device_fleet",
        "host_fleet",
    }


@pytest.mark.anyio
async def test_system_alerts_route_no_tool_alerts_when_nothing_seeded(db_session):
    from app.routes.system import system_alerts

    payload = await system_alerts()
    # An empty DB is legitimately "degraded" — the data-freshness probes
    # report integrations that have never synced. What must hold is that
    # no *tool* alerts fire with zero tool_calls rows.
    assert payload["status"] in ("all_ok", "degraded")
    assert payload["tool_alerts"] == []


@pytest.mark.anyio
async def test_tool_stats_computes_calls_errors_and_percentiles(db_session):
    from app.routes.system import tool_stats

    now = datetime.now(timezone.utc)
    for ms in (100, 200, 300, 400, 9000):
        _seed_tool_call(
            db_session, name="vault_search", status="ok",
            duration_ms=ms, called_at=now - timedelta(minutes=10),
        )
    _seed_tool_call(
        db_session, name="vault_search", status="error",
        duration_ms=50, called_at=now - timedelta(minutes=10),
    )
    db_session.commit()

    payload = await tool_stats(hours=24)
    tools = {t["name"]: t for t in payload["tools"]}

    assert "vault_search" in tools
    row = tools["vault_search"]
    assert row["calls"] == 6
    assert row["errors"] == 1
    assert row["error_rate"] == pytest.approx(1 / 6, rel=1e-3)
    assert row["p50_duration_ms"] is not None
    assert row["p95_duration_ms"] is not None
    assert row["p95_duration_ms"] >= row["p50_duration_ms"]


@pytest.mark.anyio
async def test_tool_stats_excludes_calls_outside_window(db_session):
    from app.routes.system import tool_stats

    now = datetime.now(timezone.utc)
    _seed_tool_call(
        db_session, name="ancient_tool", status="ok",
        duration_ms=100, called_at=now - timedelta(hours=48),
    )
    db_session.commit()

    payload = await tool_stats(hours=24)
    names = {t["name"] for t in payload["tools"]}
    assert "ancient_tool" not in names


@pytest.mark.anyio
async def test_tool_stats_folds_in_low_volume_errored_tool(db_session):
    """A tool outside the top-20-by-volume cut still shows up if it errored."""
    from app.routes.system import tool_stats

    now = datetime.now(timezone.utc)

    # 25 distinct high-volume tools, all healthy, to push past the top-20 cut.
    for i in range(25):
        for _ in range(30 - i):  # descending volume so ordering is deterministic
            _seed_tool_call(
                db_session, name=f"busy_tool_{i}", status="ok",
                duration_ms=100, called_at=now - timedelta(minutes=5),
            )

    # One low-volume tool with a single error — would not make top 20 by calls.
    _seed_tool_call(
        db_session, name="rare_broken_tool", status="error",
        duration_ms=100, called_at=now - timedelta(minutes=5),
    )
    db_session.commit()

    payload = await tool_stats(hours=24)
    names = [t["name"] for t in payload["tools"]]

    assert "rare_broken_tool" in names
    # Ordered by error rate desc first — the errored tool (rate 1.0) sorts
    # ahead of the healthy high-volume ones (rate 0.0).
    assert names[0] == "rare_broken_tool"


@pytest.mark.anyio
async def test_tool_stats_hours_param_is_clamped(db_session):
    from app.routes.system import tool_stats

    payload = await tool_stats(hours=999999)
    assert payload["window_hours"] <= 24 * 30
    payload = await tool_stats(hours=0)
    assert payload["window_hours"] >= 1
