"""Unit tests for the restore drill (R2, 2026-09).

No live Postgres needed: `dump_is_sane`/`find_latest_dump`/`dump_age_hours`
work on plain files, `verify_against_live` takes fake "engine" stand-ins, and
every subprocess call goes through `run_subprocess` — the one seam every
orchestration test here patches. `evaluate_alert`/`persist_result` use a tiny
in-memory fake session rather than the shared `mock_session` fixture, because
they need real filter-by-integration semantics.

Exit check (per the backlog item): a deliberately broken dump (truncated,
missing PGDMP magic) must make the drill report `ok=False` without ever
touching Postgres, and that failure must both persist to the app-readable
state and attempt a push — `test_run_drill_truncated_dump_fails_fast` and
`test_run_drill_failure_persists_and_notifies` cover exactly that.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.integrations.system import restore_drill as rd


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSyncState:
    def __init__(self, integration, last_sync_at=None, last_sync_status="never",
                 last_error=None, consecutive_failures=0):
        self.integration = integration
        self.last_sync_at = last_sync_at
        self.last_sync_status = last_sync_status
        self.last_error = last_error
        self.consecutive_failures = consecutive_failures


class _FakeQuery:
    def __init__(self, store: dict, integration: str | None):
        self._store = store
        self._integration = integration

    def filter_by(self, **kwargs):
        return _FakeQuery(self._store, kwargs.get("integration"))

    def first(self):
        return self._store.get(self._integration)


class FakeSession:
    """Enough of a Session for SyncState round-trips: query/filter_by/first,
    add, commit. `sync_states` is keyed by integration name."""

    def __init__(self):
        self.sync_states: dict[str, _FakeSyncState] = {}
        self._pending: list[_FakeSyncState] = []

    def query(self, model):
        assert model.__name__ == "SyncState"
        return _FakeQuery(self.sync_states, None)

    def add(self, obj):
        self._pending.append(obj)

    def commit(self):
        for obj in self._pending:
            self.sync_states[obj.integration] = obj
        self._pending.clear()


def _cfg(**overrides):
    base = dict(
        restore_drill_stage_dir="/does/not/matter",
        restore_drill_remote="",
        restore_drill_rclone_bin="rclone",
        restore_drill_stale_days=8,
        restore_drill_top_n_tables=10,
        restore_drill_notify_targets=[],
        daemon_silent_minutes=540,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Dump discovery + sanity
# ---------------------------------------------------------------------------


def test_find_latest_dump_picks_newest_by_name(tmp_path):
    (tmp_path / "comar-db-2026-09-01-0230.dump").write_bytes(b"x")
    newest = tmp_path / "comar-db-2026-09-03-0230.dump"
    newest.write_bytes(b"x")
    (tmp_path / "comar-db-2026-09-02-0230.dump").write_bytes(b"x")

    assert rd.find_latest_dump(tmp_path) == newest


def test_find_latest_dump_missing_dir_returns_none(tmp_path):
    assert rd.find_latest_dump(tmp_path / "nope") is None


def test_dump_is_sane_rejects_truncated_file(tmp_path):
    """The exit check's starting point: a truncated dump must fail the
    sanity gate before anything touches Postgres."""
    bad = tmp_path / "comar-db-2026-09-01-0230.dump"
    bad.write_bytes(b"PGDMP")  # right magic, far too small
    ok, reason = rd.dump_is_sane(bad)
    assert ok is False
    assert "small" in reason


def test_dump_is_sane_rejects_missing_magic(tmp_path):
    bad = tmp_path / "comar-db-2026-09-01-0230.dump"
    bad.write_bytes(b"NOTAPGDUMP" + b"0" * rd._MIN_DUMP_BYTES)
    ok, reason = rd.dump_is_sane(bad)
    assert ok is False
    assert "magic" in reason


def test_dump_is_sane_accepts_valid_dump(tmp_path):
    good = tmp_path / "comar-db-2026-09-01-0230.dump"
    good.write_bytes(rd._PGDMP_MAGIC + b"0" * rd._MIN_DUMP_BYTES)
    ok, reason = rd.dump_is_sane(good)
    assert ok is True
    assert reason is None


def test_dump_age_hours_parses_filename_timestamp():
    ts = datetime.now(timezone.utc) - timedelta(hours=25)
    name = f"comar-db-{ts.strftime('%Y-%m-%d-%H%M')}.dump"
    age = rd.dump_age_hours(Path(name))
    assert 24.5 < age < 25.5


def test_dump_age_hours_unparseable_name_returns_none():
    assert rd.dump_age_hours(Path("weird-name.dump")) is None


# ---------------------------------------------------------------------------
# verify_against_live — fake "engines"
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows=None, scalar_value=None):
        self._rows = rows or []
        self._scalar = scalar_value

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _FakeConn:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "pg_tables" in sql:
            return _FakeResult(rows=[(t,) for t in self._engine.tables])
        if "pg_stat_user_tables" in sql:
            return _FakeResult(rows=[(t,) for t in self._engine.largest])
        if "information_schema.columns" in sql:
            cols = self._engine.ts_columns.get(params["table"], [])
            return _FakeResult(rows=[(c,) for c in cols])
        if sql.strip().startswith("SELECT count(*)"):
            table = sql.split('"')[1]
            if "WHERE" in sql:
                assert params and "before" in params, sql
                return _FakeResult(scalar_value=self._engine.counts_before.get(table, 0))
            return _FakeResult(scalar_value=self._engine.counts.get(table, 0))
        raise AssertionError(f"unexpected query: {sql}")


class FakeEngine:
    def __init__(self, tables, largest, counts, ts_columns=None, counts_before=None):
        self.tables = tables
        self.largest = largest
        self.counts = counts
        # table -> timestamp column names it has (information_schema answer)
        self.ts_columns = ts_columns or {}
        # table -> count of rows at or before the as-of instant
        self.counts_before = counts_before or {}

    def connect(self):
        return _FakeConn(self)

    def dispose(self):
        pass


def test_verify_against_live_healthy_case():
    live = FakeEngine(
        tables={"a", "b"}, largest=["a", "b"],
        counts={"a": 1000, "b": 100},
    )
    drill = FakeEngine(
        tables={"a", "b"}, largest=[],
        counts={"a": 950, "b": 100},  # 95% and 100% — both within tolerance
    )
    report = rd.verify_against_live(live, drill, top_n=10)
    assert report["ok"] is True
    assert report["missing_tables"] == []
    assert report["worst_ratio"] == 0.95


def test_verify_against_live_missing_table_fails():
    live = FakeEngine(tables={"a", "b"}, largest=["a"], counts={"a": 10})
    drill = FakeEngine(tables={"a"}, largest=[], counts={"a": 10})
    report = rd.verify_against_live(live, drill, top_n=10)
    assert report["ok"] is False
    assert report["missing_tables"] == ["b"]


def test_verify_against_live_below_tolerance_fails():
    live = FakeEngine(tables={"a"}, largest=["a"], counts={"a": 1000})
    drill = FakeEngine(tables={"a"}, largest=[], counts={"a": 500})  # 50%
    report = rd.verify_against_live(live, drill, top_n=10)
    assert report["ok"] is False
    assert report["checked_tables"][0]["ok"] is False
    assert report["worst_ratio"] == 0.5


def test_verify_against_live_drill_exceeding_live_fails():
    """A restored snapshot must never have MORE rows than the live table it
    was compared to — that means the wrong database was compared, not a
    healthy surplus. See module docstring."""
    live = FakeEngine(tables={"a"}, largest=["a"], counts={"a": 100})
    drill = FakeEngine(tables={"a"}, largest=[], counts={"a": 150})
    report = rd.verify_against_live(live, drill, top_n=10)
    assert report["ok"] is False
    assert report["checked_tables"][0]["ok"] is False


# ---------------------------------------------------------------------------
# evaluate_alert — the system_alerts axis
# ---------------------------------------------------------------------------


def test_evaluate_alert_never_run_is_not_silently_ok():
    session = FakeSession()
    result = rd.evaluate_alert(session, stale_days=8)
    assert result["last_outcome"] == "never_run"
    assert result["days_since_restore_drill"] is None
    assert result["issues"]  # never silently healthy


def test_evaluate_alert_recent_success_has_no_issues():
    session = FakeSession()
    session.sync_states["restore_drill"] = _FakeSyncState(
        "restore_drill",
        last_sync_at=datetime.now(timezone.utc) - timedelta(days=1),
        last_sync_status="ok",
    )
    result = rd.evaluate_alert(session, stale_days=8)
    assert result["last_outcome"] == "ok"
    assert result["issues"] == []


def test_evaluate_alert_stale_success_degrades():
    session = FakeSession()
    session.sync_states["restore_drill"] = _FakeSyncState(
        "restore_drill",
        last_sync_at=datetime.now(timezone.utc) - timedelta(days=10),
        last_sync_status="ok",
    )
    result = rd.evaluate_alert(session, stale_days=8)
    assert any("stale" in i for i in result["issues"])


def test_evaluate_alert_failed_run_degrades_even_if_recent():
    session = FakeSession()
    session.sync_states["restore_drill"] = _FakeSyncState(
        "restore_drill",
        last_sync_at=datetime.now(timezone.utc) - timedelta(hours=2),
        last_sync_status="error",
    )
    result = rd.evaluate_alert(session, stale_days=8)
    assert any("FAILED" in i for i in result["issues"])


# ---------------------------------------------------------------------------
# push_failure_notify — reuses homeassistant.notify, never a third channel
# ---------------------------------------------------------------------------


def test_push_failure_notify_calls_ha_notify_capability(monkeypatch):
    fake_notify = MagicMock()
    monkeypatch.setattr(
        rd, "get_capability", lambda name: fake_notify if name == "homeassistant.notify" else None
    )
    monkeypatch.setattr(rd, "plugin_config", lambda name: _cfg(restore_drill_notify_targets=["a_phone"]))

    rd.push_failure_notify(rd.DrillResult(ok=False, dump_name="comar-db-x.dump", reason="boom"))

    assert fake_notify.notify.call_count == 1
    args, kwargs = fake_notify.notify.call_args
    assert args[0] == "a_phone"
    assert "FAILED" in args[1]
    assert "boom" in args[2]
    assert kwargs["data"]["push"]["interruption-level"] == "critical"


def test_push_failure_notify_noop_when_no_targets_configured(monkeypatch):
    fake_notify = MagicMock()
    monkeypatch.setattr(rd, "get_capability", lambda name: fake_notify)
    monkeypatch.setattr(rd, "plugin_config", lambda name: _cfg(restore_drill_notify_targets=[]))

    rd.push_failure_notify(rd.DrillResult(ok=False, reason="boom"))

    fake_notify.notify.assert_not_called()


# ---------------------------------------------------------------------------
# run_drill orchestration — the exit check
# ---------------------------------------------------------------------------


def test_run_drill_no_dump_found_fails_and_notifies(tmp_path, monkeypatch):
    session = FakeSession()
    fake_notify = MagicMock()
    monkeypatch.setattr(rd, "get_capability", lambda name: fake_notify)
    monkeypatch.setattr(
        rd, "plugin_config",
        lambda name: _cfg(restore_drill_stage_dir=str(tmp_path), restore_drill_notify_targets=["a_phone"]),
    )

    result = rd.run_drill(session)

    assert result.ok is False
    assert "no dump found" in result.reason
    assert session.sync_states["restore_drill"].last_sync_status == "error"
    fake_notify.notify.assert_called_once()


def test_run_drill_truncated_dump_fails_fast_without_touching_postgres(tmp_path, monkeypatch):
    """The literal exit check: feed the drill a truncated file, it must
    report `failed` and never shell out to psql/pg_restore/createdb."""
    bad = tmp_path / "comar-db-2026-09-01-0230.dump"
    bad.write_bytes(b"PGDMP")  # magic present, size is not

    session = FakeSession()
    monkeypatch.setattr(
        rd, "plugin_config",
        lambda name: _cfg(restore_drill_stage_dir=str(tmp_path), restore_drill_notify_targets=["a_phone"]),
    )
    fake_notify = MagicMock()
    monkeypatch.setattr(rd, "get_capability", lambda name: fake_notify)

    subprocess_spy = MagicMock()
    monkeypatch.setattr(rd, "run_subprocess", subprocess_spy)

    result = rd.run_drill(session)

    assert result.ok is False
    assert "small" in result.reason
    assert result.dump_name == bad.name
    subprocess_spy.assert_not_called()  # never reached create/restore
    assert session.sync_states["restore_drill"].last_sync_status == "error"
    fake_notify.notify.assert_called_once()


def test_run_drill_success_persists_ok_and_does_not_notify(tmp_path, monkeypatch):
    good = tmp_path / "comar-db-2026-09-01-0230.dump"
    good.write_bytes(rd._PGDMP_MAGIC + b"0" * rd._MIN_DUMP_BYTES)

    session = FakeSession()
    monkeypatch.setattr(rd, "plugin_config", lambda name: _cfg(restore_drill_stage_dir=str(tmp_path)))
    fake_notify = MagicMock()
    monkeypatch.setattr(rd, "get_capability", lambda name: fake_notify)

    ok_proc = SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(rd, "run_subprocess", lambda *a, **k: ok_proc)

    live = FakeEngine(tables={"a"}, largest=["a"], counts={"a": 100})
    drill = FakeEngine(tables={"a"}, largest=[], counts={"a": 100})

    def fake_create_engine(url):
        return drill if "lios_drill_" in url else live

    monkeypatch.setattr("sqlalchemy.create_engine", fake_create_engine)
    monkeypatch.setattr(rd, "_current_database_url", lambda: "postgresql://x/home_services")

    result = rd.run_drill(session)

    assert result.ok is True
    assert session.sync_states["restore_drill"].last_sync_status == "ok"
    fake_notify.notify.assert_not_called()
    # The persisted JSON summary carries the fields the alert axis and any
    # human reviewer need (dump name, age, ratio) — see persist_result's
    # docstring for why `last_error` is reused for this on both outcomes.
    summary = json.loads(session.sync_states["restore_drill"].last_error)
    assert summary["dump_name"] == good.name
    assert summary["worst_ratio"] == 1.0


# ─── client/server version pre-flight ────────────────────────────────────────


def _proc(stdout):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_major_parses_client_and_server_forms():
    assert rd._major("pg_restore (PostgreSQL) 17.11 (Debian 17.11-0+deb13u1)") == 17
    assert rd._major("160013") == 16
    assert rd._major("") is None


def test_run_drill_fails_fast_on_client_server_major_mismatch(tmp_path, monkeypatch):
    """The first live run (2026-09-04): image pg_restore 17, server 16 — the
    drill must name that, not report a bare `pg_restore failed`, and must
    never create the scratch database."""
    good = tmp_path / "comar-db-2026-09-01-0230.dump"
    good.write_bytes(rd._PGDMP_MAGIC + b"0" * rd._MIN_DUMP_BYTES)
    session = FakeSession()
    monkeypatch.setattr(
        rd, "plugin_config",
        lambda name: _cfg(restore_drill_stage_dir=str(tmp_path), restore_drill_notify_targets=["a_phone"]),
    )
    fake_notify = MagicMock()
    monkeypatch.setattr(rd, "get_capability", lambda name: fake_notify)
    monkeypatch.setattr(rd, "_current_database_url", lambda: "postgresql://x/home_services")

    calls = []

    def fake_run(args, **kw):
        calls.append(args[0])
        if args[0] == "pg_restore" and "--version" in args:
            return _proc("pg_restore (PostgreSQL) 17.11 (Debian 17.11-0+deb13u1)")
        if args[0] == "psql":
            return _proc("160013\n")
        raise AssertionError(f"unexpected subprocess after a mismatch: {args}")

    monkeypatch.setattr(rd, "run_subprocess", fake_run)
    result = rd.run_drill(session)
    assert result.ok is False
    assert "client is PostgreSQL 17" in result.reason and "server is 16" in result.reason
    assert "createdb" not in calls
    fake_notify.notify.assert_called_once()


def test_version_preflight_unknown_does_not_block(monkeypatch):
    monkeypatch.setattr(rd, "run_subprocess", lambda *a, **k: _proc(""))
    assert rd.client_server_version_mismatch("postgresql://x/db") is None


# ─── volatile tables are not a fidelity signal ───────────────────────────────


def test_largest_tables_skips_volatile_and_takes_the_next_stable_one():
    """First live run (2026-09-04): embedding_queue 1,105 at dump, 8,871 at
    drill — ratio 0.12 — while every real table matched to 0.1%. A queue's
    count is not what a restore drill measures."""
    live = FakeEngine(tables={"q", "a", "b"}, largest=["q", "a", "b"], counts={})
    assert rd._largest_tables(live, 2, exclude=["q"]) == ["a", "b"]
    assert rd._largest_tables(live, 2) == ["q", "a"]


def test_verify_against_live_ignores_a_volatile_queue():
    live = FakeEngine(tables={"embedding_queue", "a"}, largest=["embedding_queue", "a"],
                      counts={"embedding_queue": 8871, "a": 1000})
    drill = FakeEngine(tables={"embedding_queue", "a"}, largest=[],
                       counts={"embedding_queue": 1105, "a": 990})
    report = rd.verify_against_live(live, drill, top_n=10, exclude=["embedding_queue"])
    assert report["ok"] is True
    assert [t["table"] for t in report["checked_tables"]] == ["a"]


def test_default_volatile_list_names_embedding_queue():
    from app.integrations.system.manifest import MANIFEST
    spec = MANIFEST.config_schema["restore_drill_volatile_tables"]
    assert "embedding_queue" in spec.default


# ─── retention prunes shrink live between dump and drill (2026-09-06) ────────


def test_verify_against_live_tolerates_a_table_pruned_since_the_dump():
    """The 2026-09-06 failure: dump at 02:30, kernel retention prunes at
    03:00, drill at 04:00. A pruned ledger (`runs`, `client_logs`, …) then
    holds slightly FEWER rows live than in the dump — `drill > live` by one
    night's pruning — and the one-sided rule failed the drill with every
    checked table within 1.3% of live. Inside the band, that is healthy."""
    live = FakeEngine(tables={"runs", "a"}, largest=["runs", "a"],
                      counts={"runs": 40_000, "a": 1_000})
    drill = FakeEngine(tables={"runs", "a"}, largest=[],
                       counts={"runs": 40_400, "a": 990})  # 1.01 and 0.99
    report = rd.verify_against_live(live, drill, top_n=10)
    assert report["ok"] is True
    assert report["failures"] == []
    by_table = {c["table"]: c for c in report["checked_tables"]}
    assert by_table["runs"]["ok"] is True
    assert by_table["runs"]["ratio"] == 1.01


def test_verify_against_live_worst_ratio_is_furthest_from_one_in_either_direction():
    """A drill exceeding live by 50% must not read as a healthy worst ratio
    of 1.0 just because the minimum happened to be the other table."""
    live = FakeEngine(tables={"a", "b"}, largest=["a", "b"], counts={"a": 100, "b": 100})
    drill = FakeEngine(tables={"a", "b"}, largest=[], counts={"a": 150, "b": 100})
    report = rd.verify_against_live(live, drill, top_n=10)
    assert report["ok"] is False
    assert report["worst_ratio"] == 1.5
    # Above the band is still the wrong-database guard — exactly as before,
    # only the threshold moved from "any surplus" to "more than 10%".
    assert rd.RESTORE_DRILL_HIGH_TOLERANCE == 1.1
    assert report["failures"] == ["a drill 150 vs live 100 (ratio 1.5)"]


def test_run_drill_mismatch_reason_names_the_table(tmp_path, monkeypatch):
    """`system_alerts` shows only the first 200 characters of the persisted
    summary, so the reason must lead with the offending table and both
    counts — the 2026-09-06 alert showed `worst_ratio 0.9877` and nothing
    that said which table, or that the surplus was on the drill side."""
    good = tmp_path / "comar-db-2026-09-01-0230.dump"
    good.write_bytes(rd._PGDMP_MAGIC + b"0" * rd._MIN_DUMP_BYTES)
    session = FakeSession()
    monkeypatch.setattr(rd, "plugin_config", lambda name: _cfg(restore_drill_stage_dir=str(tmp_path)))
    monkeypatch.setattr(rd, "get_capability", lambda name: MagicMock())
    monkeypatch.setattr(rd, "run_subprocess", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    live = FakeEngine(tables={"a", "b"}, largest=["a", "b"], counts={"a": 1000, "b": 100})
    drill = FakeEngine(tables={"a", "b"}, largest=[], counts={"a": 1000, "b": 50})
    monkeypatch.setattr("sqlalchemy.create_engine", lambda url: drill if "lios_drill_" in url else live)
    monkeypatch.setattr(rd, "_current_database_url", lambda: "postgresql://x/home_services")

    result = rd.run_drill(session)

    assert result.ok is False
    assert result.reason.startswith("row count mismatch: b drill 50 vs live 100 (ratio 0.5)")
    assert "worst ratio 0.5" in result.reason


def test_verify_against_live_counts_as_of_the_dump_when_a_table_is_dated():
    """The 2026-09-06 11:03 hand-run drill: `runs` had 6,166 rows at the 02:30
    dump and 7,082 live eight hours later — all of them tool calls made after
    the dump. Whole-table ratio 0.87 fails the band; bounded to the dump's
    timestamp, both sides hold 6,166 and the restore is exactly right."""
    as_of = datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)
    live = FakeEngine(tables={"runs", "a"}, largest=["runs", "a"],
                      counts={"runs": 7_082, "a": 1_000},
                      ts_columns={"runs": ["started_at", "finished_at"]},
                      counts_before={"runs": 6_166})
    drill = FakeEngine(tables={"runs", "a"}, largest=[],
                       counts={"runs": 6_166, "a": 1_000},
                       counts_before={"runs": 6_166})

    without = rd.verify_against_live(live, drill, top_n=10)
    assert without["ok"] is False, "whole-table ratio 0.87 must still fail without as_of"

    report = rd.verify_against_live(live, drill, top_n=10, as_of=as_of)
    assert report["ok"] is True, report
    by_table = {c["table"]: c for c in report["checked_tables"]}
    assert by_table["runs"] == {"table": "runs", "live": 6_166, "drill": 6_166, "ratio": 1.0,
                                "ok": True, "as_of_column": "started_at"}
    assert "as_of_column" not in by_table["a"]  # undated table: compared whole, as before


def test_as_of_count_still_catches_a_truncated_restore():
    """Bounding by time must not hide a real loss: rows that existed at dump
    time but did not come back are exactly what the drill is for."""
    as_of = datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)
    live = FakeEngine(tables={"runs"}, largest=["runs"], counts={"runs": 7_082},
                      ts_columns={"runs": ["started_at"]}, counts_before={"runs": 6_166})
    drill = FakeEngine(tables={"runs"}, largest=[], counts={"runs": 3_000},
                       counts_before={"runs": 3_000})
    report = rd.verify_against_live(live, drill, top_n=10, as_of=as_of)
    assert report["ok"] is False
    assert report["failures"] == ["runs drill 3000 vs live 6166 (ratio 0.4865)"]


def test_timestamp_column_prefers_arrival_columns_in_order():
    eng = FakeEngine(tables=set(), largest=[], counts={},
                     ts_columns={"t": ["finished_at", "created_at", "started_at"]})
    assert rd._timestamp_column(eng, "t") == "started_at"
    assert rd._timestamp_column(eng, "no_such") is None


def test_dump_timestamp_parses_and_is_utc():
    ts = rd.dump_timestamp(Path("comar-db-2026-09-06-0230.dump"))
    assert ts == datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)
    assert rd.dump_timestamp(Path("whatever.dump")) is None
