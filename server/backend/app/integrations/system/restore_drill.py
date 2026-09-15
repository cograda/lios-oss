"""Weekly restore drill — proves the nightly Postgres dump is *restorable*,
not just present.

Backlog item (`vault/Projects/lios/Backlog.md` § Next, R2, 2026-09):
"Close the two silent-failure gaps: restore drill and daemon heartbeat".
Dumps have stopped silently before (2026-07-14) — a backup nobody has ever
restored is a hope, not a backup.

Runs **inside the app container** (`docker compose exec app python -m
app.integrations.system.restore_drill`), invoked by a host cron — see
`deploy/scripts/lios-restore-drill.sh` for the wrapper and
`deploy/Makefile`'s `restore-drill-install` target for the cron line. That
placement (rather than a pure host-side bash script) is deliberate: it lets
the drill write its result straight to `SyncState` via a normal DB session
(the same table every other integration's sync uses — see
`app/scheduler.py::_update_sync_state`), so `system_alerts` reads it exactly
like any other integration's freshness, and it makes the comparison logic
unit-testable in the normal pytest suite (`tests/test_restore_drill.py`)
without needing a live Postgres — the sanity/comparison functions below take
plain paths and SQLAlchemy engines, and every subprocess call goes through
the single `run_subprocess` seam so tests can patch it.

Steps, per run:
1. Find the latest `comar-db-*.dump` in the staging dir (falls back to
   pulling the newest one from the Drive remote if the staging copy is gone
   — same remote `lios-db-backup.sh`/`lios-db-restore.sh` already use).
2. Sanity-gate it exactly like `lios-db-backup.sh` does before trusting a
   dump it just made (PGDMP magic, minimum size) — a truncated file must
   fail here, not three steps deeper inside `pg_restore`.
3. `pg_restore` it into a **scratch database** (`lios_drill_<epoch>`),
   created and dropped by this module — never the live `home_services` DB.
4. Assert against the live database: the drill DB has every table the live
   DB has, and for the ten largest live tables (by `pg_stat_user_tables`
   estimate — cheap, no full scan), the drill's row count is within
   tolerance of the live count.
5. Write the result to `SyncState` (integration=`restore_drill`) and, on
   failure, push via the same HA REST mechanism `lios-db-backup.sh`'s
   `notify_failure` uses — a `notify.<target>` service call — through the
   existing `homeassistant.notify` capability rather than re-implementing
   the env-file-reading + curl logic in Python a second time.

⚠️ Tolerance, and why it's a band rather than equality: the drill runs
hours (or a day) after the dump was taken, so the live DB has kept moving
while the dump is a frozen snapshot. The comparison therefore accepts a
drill/live ratio anywhere in `[RESTORE_DRILL_LOW_TOLERANCE,
RESTORE_DRILL_HIGH_TOLERANCE]` = `[0.9, 1.1]` for each of the ten largest
tables. Both bounds are documented rather than derived, because there is no
principled way to compute "how much a day of comar's write volume is" from
first principles — 10% was chosen to catch a truly broken restore (a handful
of tables at 0%, or a comparison against the wrong database entirely) while
tolerating one day of ordinary movement in either direction.

Why the band is two-sided (2026-09-06): the first version of this rule
treated `drill > live` as a hard failure on the reasoning that an
append-mostly table can never legitimately lose rows between dump and
drill. It can. The nightly dump runs at 02:30, the kernel's retention prunes
(`app/plugin/kernel_jobs.py` — tool-call `runs` rows at 30 days,
`client_logs` at 30, `auth_events` and scheduled `runs` at 90,
`ha_entity_churn` at 180) run at 03:00, and the drill at 04:00 — so every
table with a rolling retention window holds *fewer* rows live than in the
dump, by exactly one night's pruning, and the 2026-09-06 drill failed on
that with every checked table within 1.3% of live. A shrink of a day's worth
of rows is the steady state for a pruned ledger, not a wrong-database
signal; the wrong database is off by far more than 10%. Excluding the pruned
tables via `restore_drill_volatile_tables` was rejected because it would take
the runs ledger out of the fidelity check altogether and need re-doing for
every table that ever gains a retention job.

Why the count is bounded to the dump's timestamp (2026-09-06, later the same
day): the two-sided band fixed the 04:00 cron run and then the first hand-run
drill at 11:03 failed anyway — `runs drill 6166 vs live 7082 (ratio 0.87)`.
Nothing was wrong with the restore: the ledger had simply gained 900 tool
calls in the eight and a half hours since the dump. A ratio cannot separate
"rows added after the snapshot" from "rows lost in the restore", and the
wider the gap between dump and drill the worse it gets — so the drill now
counts, on both sides, only rows whose arrival timestamp (`started_at`,
`created_at`, … — see `_AS_OF_COLUMNS`) is at or before the dump's own
timestamp. A table with no such column is still compared whole. The band
stays, for the prune shrink above and for whatever has no timestamp.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

RESTORE_DRILL_INTEGRATION = "restore_drill"
SCRATCH_DB_PREFIX = "lios_drill_"

# Matches lios-db-backup.sh's own sanity gate exactly — see its comment
# ("A truncated/empty dump that silently uploads is worse than a loud
# failure"). Duplicated rather than shared because the two run in different
# languages/processes; keep them in step if either changes.
_PGDMP_MAGIC = b"PGDMP"
_MIN_DUMP_BYTES = 1_000_000

_DUMP_NAME_RE = re.compile(r"comar-db-(\d{4}-\d{2}-\d{2}-\d{4})\.dump$")

RESTORE_DRILL_LOW_TOLERANCE = 0.9  # drill must hold >= 90% of live's rows ...
RESTORE_DRILL_HIGH_TOLERANCE = 1.1  # ... and <= 110% (retention prunes shrink live; see module docstring)
# The stale-days and top-N-tables defaults live in one place only:
# manifest.py::MANIFEST.config_schema (`restore_drill_stale_days` = 8,
# `restore_drill_top_n_tables` = 10), read via `plugin_config("system")`.


@dataclass
class DrillResult:
    ok: bool
    dump_name: str | None = None
    dump_age_hours: float | None = None
    table_count: int | None = None
    live_table_count: int | None = None
    worst_ratio: float | None = None
    reason: str | None = None
    checked_tables: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dump_name": self.dump_name,
            "dump_age_hours": self.dump_age_hours,
            "table_count": self.table_count,
            "live_table_count": self.live_table_count,
            "worst_ratio": self.worst_ratio,
            "reason": self.reason,
            "checked_tables": self.checked_tables,
        }


# ---------------------------------------------------------------------------
# Dump discovery + sanity
# ---------------------------------------------------------------------------


def find_latest_dump(stage_dir: Path) -> Path | None:
    """Newest `comar-db-*.dump` in the staging dir, or None.

    Filenames are `comar-db-<YYYY-MM-DD-HHMM>.dump` (`lios-db-backup.sh`),
    which sorts correctly as a plain string — no need to parse the
    timestamp just to pick the latest.
    """
    if not stage_dir.is_dir():
        return None
    candidates = sorted(stage_dir.glob("comar-db-*.dump"))
    return candidates[-1] if candidates else None


def pull_latest_from_remote(
    stage_dir: Path, *, rclone_bin: str, remote: str
) -> Path | None:
    """Best-effort: fetch the newest dump from the Drive remote when the
    staging copy is gone (the exact scenario `lios-db-restore.sh` already
    handles for a manual restore). Never raises — a remote outage here means
    "no dump found", not a crash of the whole drill.
    """
    try:
        proc = run_subprocess([rclone_bin, "lsf", f"{remote}/", "--include", "comar-db-*.dump"])
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        names = sorted(line.strip() for line in proc.stdout.splitlines() if line.strip())
        latest = names[-1]
        stage_dir.mkdir(parents=True, exist_ok=True)
        proc = run_subprocess([rclone_bin, "copy", f"{remote}/{latest}", str(stage_dir)])
        if proc.returncode != 0:
            return None
        dest = stage_dir / latest
        return dest if dest.exists() else None
    except Exception:  # noqa: BLE001
        logger.exception("restore drill: fetching latest dump from remote failed")
        return None


def dump_is_sane(path: Path) -> tuple[bool, str | None]:
    """The same two checks `lios-db-backup.sh` runs on a dump it just made."""
    try:
        size = path.stat().st_size
    except OSError as e:
        return False, f"cannot stat dump: {e}"
    if size < _MIN_DUMP_BYTES:
        return False, f"dump suspiciously small ({size} bytes, min {_MIN_DUMP_BYTES})"
    with path.open("rb") as f:
        head = f.read(len(_PGDMP_MAGIC))
    if head != _PGDMP_MAGIC:
        return False, "dump missing PGDMP magic"
    return True, None


def dump_timestamp(path: Path) -> datetime | None:
    """When the dump was taken, read from its own filename — not mtime, which
    a copy/rclone transfer can reset. UTC, because the backup script names
    the file from `date -u`."""
    m = _DUMP_NAME_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d-%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def dump_age_hours(path: Path) -> float | None:
    """Age of the dump in hours, from `dump_timestamp`."""
    dumped_at = dump_timestamp(path)
    if dumped_at is None:
        return None
    return (datetime.now(timezone.utc) - dumped_at).total_seconds() / 3600


# ---------------------------------------------------------------------------
# Scratch database lifecycle — subprocess, one seam (`run_subprocess`)
# ---------------------------------------------------------------------------


def run_subprocess(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    """Thin wrapper so tests patch exactly one thing."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _with_dbname(database_url: str, dbname: str) -> str:
    """Swap the dbname in a Postgres URL — used to reach the server's admin
    db (`postgres`) for CREATE/DROP DATABASE, and the scratch db for restore
    and verification."""
    parts = urlsplit(database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _major(version_text: str) -> int | None:
    """First integer in `pg_restore (PostgreSQL) 17.11 (Debian …)` or a
    `server_version_num` like `160013` → 17 / 16. None if unparseable."""
    m = re.search(r"(\d+)", version_text or "")
    if not m:
        return None
    n = int(m.group(1))
    return n // 10000 if n >= 10000 else n


def client_server_version_mismatch(database_url: str) -> str | None:
    """Pre-flight for the one failure the first live run hit (2026-09-04):
    the app image is Debian 13, whose `postgresql-client` is 17; the server
    is 16. A 17 `pg_restore` emits `SET transaction_timeout = 0`, which a 16
    server rejects, and the drill reported a bare `pg_restore failed`. So
    compare majors first and name the mismatch. Both probes go through
    `run_subprocess` (the module's one seam); if either is unparseable the
    check returns None and the drill proceeds — `pg_restore` itself will
    still fail loudly on a real mismatch, so "unknown" never masks one.
    """
    client = run_subprocess(["pg_restore", "--version"], timeout=30)
    server = run_subprocess(
        ["psql", database_url, "-tAc", "show server_version_num"], timeout=30,
    )
    c, s_ = _major(client.stdout), _major(server.stdout)
    if c is None or s_ is None:
        return None
    if c != s_:
        return (
            f"pg_restore client is PostgreSQL {c} but the server is {s_}; the image's "
            f"postgresql-client must match the server major (see core/server/Dockerfile)"
        )
    return None


def create_scratch_db(database_url: str, drill_db: str) -> None:
    admin_url = _with_dbname(database_url, "postgres")
    proc = run_subprocess(
        ["psql", admin_url, "-v", "ON_ERROR_STOP=1", "-c", f'CREATE DATABASE "{drill_db}"']
    )
    if proc.returncode != 0:
        raise RuntimeError(f"create scratch db failed: {proc.stderr.strip()[:500]}")


def drop_scratch_db(database_url: str, drill_db: str) -> None:
    """Best-effort teardown — never the live DB (name is always our own
    `SCRATCH_DB_PREFIX`-prefixed one), never raises so a failed drop doesn't
    mask the drill's real result."""
    admin_url = _with_dbname(database_url, "postgres")
    run_subprocess([
        "psql", admin_url, "-v", "ON_ERROR_STOP=0", "-c",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{drill_db}'",
    ])
    proc = run_subprocess(
        ["psql", admin_url, "-v", "ON_ERROR_STOP=1", "-c", f'DROP DATABASE IF EXISTS "{drill_db}"']
    )
    if proc.returncode != 0:
        logger.error("restore drill: dropping scratch db %s failed: %s", drill_db, proc.stderr.strip())


def restore_dump(database_url: str, drill_db: str, dump_path: Path) -> None:
    target_url = _with_dbname(database_url, drill_db)
    proc = run_subprocess([
        "pg_restore", "--no-owner", "--exit-on-error", "-d", target_url, str(dump_path),
    ], timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_restore failed (exit {proc.returncode}): {proc.stderr.strip()[-2000:]}")


# ---------------------------------------------------------------------------
# Verification — plain SQLAlchemy engines, so tests can pass fakes
# ---------------------------------------------------------------------------


def _table_names(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).fetchall()
    return {r[0] for r in rows}


def _largest_tables(engine: Engine, top_n: int, exclude: list[str] | None = None) -> list[str]:
    """Names of the `top_n` largest live tables, by `pg_stat_user_tables`'s
    live-tuple estimate — an index/stats lookup, never a full table scan.

    `exclude` is the configured volatile set (`restore_drill_volatile_tables`):
    work queues whose row count moves by an order of magnitude between the
    dump and the drill are not a fidelity signal. They are filtered here, in
    Python, after over-fetching by their count, so the check falls to the
    next-largest *stable* table rather than shrinking the sample."""
    excluded = set(exclude or [])
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT relname FROM pg_stat_user_tables "
                "ORDER BY n_live_tup DESC LIMIT :n"
            ),
            {"n": top_n + len(excluded)},
        ).fetchall()
    return [r[0] for r in rows if r[0] not in excluded][:top_n]


# Columns that, when present on a table, date each row's arrival. Checked in
# this order; the first one the table has wins. A table with none of them is
# compared by its whole count, as before.
_AS_OF_COLUMNS = ("started_at", "created_at", "occurred_at", "received_at", "recorded_at", "timestamp")


def _timestamp_column(engine: Engine, table: str) -> str | None:
    """The column to bound an as-of-dump count on, or None if the table has
    no row-arrival timestamp. One information_schema lookup per table."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table "
                "AND data_type LIKE 'timestamp%'"
            ),
            {"table": table},
        ).fetchall()
    present = {r[0] for r in rows}
    for name in _AS_OF_COLUMNS:
        if name in present:
            return name
    return None


def _row_count(engine: Engine, table: str, *, before: datetime | None = None, column: str | None = None) -> int:
    """Row count, bounded to rows that existed at `before` when the table has
    a `column` dating them. Both sides of the comparison get the same bound,
    so a ledger that has gained eight hours of rows since the dump compares
    equal — which is the fidelity question the drill is actually asking."""
    with engine.connect() as conn:
        if before is not None and column:
            return conn.execute(
                text(f'SELECT count(*) FROM "{table}" WHERE "{column}" <= :before'),
                {"before": before},
            ).scalar() or 0
        return conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar() or 0


def verify_against_live(
    live_engine: Engine,
    drill_engine: Engine,
    *,
    top_n: int,
    exclude: list[str] | None = None,
    as_of: datetime | None = None,
) -> dict:
    """The assertion at the heart of the drill: same table set, and the
    `top_n` largest tables within the `[LOW, HIGH]` tolerance band. See the
    module docstring for why the band is two-sided — a pruned ledger is
    legitimately *smaller* live than in the dump, so `drill > live` is only a
    failure once it exceeds the same 10% that bounds the other side.

    `as_of` is the dump's timestamp. When given, any checked table with a
    row-arrival column (`_AS_OF_COLUMNS`) is counted on BOTH sides only up to
    that instant, so rows the live database gained after the dump do not
    count against the restore. This is what the 2026-09-06 hand-run drill
    needed: the `runs` ledger held 6,166 rows at the 02:30 dump and 7,082 by
    the 11:03 drill — a 13% gap made entirely of tool calls that happened
    after the dump, which no tolerance band can tell apart from a truncated
    restore. Bounding by time can. Retention prunes still make the live
    side slightly *smaller* (they delete old rows that are in the dump), and
    the band absorbs that as before.

    `failures` lists every out-of-band table with both counts, so the
    persisted reason names the table rather than only the worst ratio —
    `system_alerts` truncates `last_error` to 200 characters, and the
    2026-09-06 failure was undiagnosable from the alert because the offending
    table sat past that cut in `checked_tables`.
    """
    live_tables = _table_names(live_engine)
    drill_tables = _table_names(drill_engine)
    missing = sorted(live_tables - drill_tables)
    extra = sorted(drill_tables - live_tables)

    largest = _largest_tables(live_engine, top_n, exclude)
    checked: list[dict] = []
    worst_ratio = 1.0
    for table in largest:
        column = _timestamp_column(live_engine, table) if as_of is not None else None
        live_n = _row_count(live_engine, table, before=as_of, column=column)
        try:
            drill_n = _row_count(drill_engine, table, before=as_of, column=column)
        except Exception as e:  # noqa: BLE001
            checked.append({"table": table, "live": live_n, "drill": None, "ok": False, "error": str(e)})
            worst_ratio = 0.0
            continue
        if live_n == 0:
            ratio = 1.0 if drill_n == 0 else 0.0
        else:
            ratio = drill_n / live_n
        ok = RESTORE_DRILL_LOW_TOLERANCE <= ratio <= RESTORE_DRILL_HIGH_TOLERANCE
        entry = {"table": table, "live": live_n, "drill": drill_n, "ratio": round(ratio, 4), "ok": ok}
        if column:
            entry["as_of_column"] = column
        checked.append(entry)
        # `worst_ratio` is the furthest from 1.0 in either direction, so a
        # drill that *exceeds* live by 50% reads as 1.5, not as a healthy 1.0.
        if abs(ratio - 1.0) > abs(worst_ratio - 1.0):
            worst_ratio = ratio

    failures = [
        f"{c['table']} drill {c['drill']} vs live {c['live']}"
        + (f" (ratio {c['ratio']})" if c.get("ratio") is not None else f" ({c.get('error')})")
        for c in checked if not c["ok"]
    ]
    return {
        "missing_tables": missing,
        "extra_tables": extra,
        "checked_tables": checked,
        "failures": failures,
        "worst_ratio": round(worst_ratio, 4),
        "live_table_count": len(live_tables),
        "drill_table_count": len(drill_tables),
        "ok": not missing and all(c["ok"] for c in checked),
    }


# ---------------------------------------------------------------------------
# Persistence + notification
# ---------------------------------------------------------------------------


def persist_result(session: Session, result: DrillResult) -> None:
    """Write to `SyncState`, same table/pattern every integration's sync
    uses (`app/scheduler.py::_update_sync_state`) — that's what lets
    `system_alerts` read `days_since_restore_drill` off `last_sync_at` for
    free. `last_error` is reused to hold the structured JSON summary (dump
    name/age, table count, worst ratio) on *both* outcomes, not just
    failure — there is no dedicated column for it and the alerts axis needs
    those fields on a healthy run too, so the field's usual "only set on
    error" convention is deliberately broken here; this docstring is the
    warning for anyone who goes looking for it as an actual error string.
    """
    from app.models.tokens import SyncState

    state = session.query(SyncState).filter_by(integration=RESTORE_DRILL_INTEGRATION).first()
    if state is None:
        state = SyncState(integration=RESTORE_DRILL_INTEGRATION)
        session.add(state)
    state.last_sync_at = datetime.now(timezone.utc)
    state.last_sync_status = "ok" if result.ok else "error"
    state.last_error = json.dumps(result.to_dict())
    state.consecutive_failures = 0 if result.ok else (state.consecutive_failures or 0) + 1
    session.commit()


def push_failure_notify(result: DrillResult) -> None:
    """Push a failure alert via the same mechanism `lios-db-backup.sh`'s
    `notify_failure` uses: a critical HA `notify.<target>` service call.
    That script POSTs to HA's REST API directly from the host, using an
    env file (`notify.env`) for `HA_URL`/`HA_TOKEN`/`NOTIFY_SERVICE`. This
    module runs inside the app container instead, so it reaches the exact
    same HA REST endpoint through the existing `homeassistant.notify`
    capability (`app/integrations/homeassistant/facade.py::notify`, which
    calls `POST .../api/services/notify/<target>`) rather than duplicating
    the env-file-reading + curl logic in Python — "reuse it, do not invent
    a third [channel]" per the backlog item.
    """
    cfg = plugin_config("system")
    targets = cfg.restore_drill_notify_targets or []
    if not targets:
        logger.warning(
            "restore drill failed but system.restore_drill_notify_targets is "
            "empty — cannot push (see system_alerts for the failure instead)"
        )
        return
    try:
        notify = get_capability("homeassistant.notify")
    except Exception:  # noqa: BLE001
        logger.exception("restore drill: homeassistant.notify capability unavailable")
        return

    title = "lios: restore drill FAILED"
    body = result.reason or "restore drill failed"
    if result.dump_name:
        body = f"{body} (dump {result.dump_name})"
    for target in targets:
        try:
            notify.notify(target, title, body, data={"push": {"interruption-level": "critical"}})
        except Exception:  # noqa: BLE001
            logger.exception("restore drill: notify push to %s failed", target)


def evaluate_alert(session: Session, *, stale_days: int | None = None) -> dict:
    """The `system_alerts` axis: days since the last run and its outcome.

    Read off `SyncState` — same honesty rule as every other axis in this
    package: no probe ever having run is reported as `never_run`, not folded
    into a falsely-reassuring `ok`.
    """
    from app.models.tokens import SyncState

    if stale_days is None:
        stale_days = plugin_config("system").restore_drill_stale_days

    state = session.query(SyncState).filter_by(integration=RESTORE_DRILL_INTEGRATION).first()
    if state is None or state.last_sync_at is None:
        return {
            "days_since_restore_drill": None,
            "last_outcome": "never_run",
            "issues": ["restore drill never run — no tested restore exists"],
        }

    days_since = (datetime.now(timezone.utc) - state.last_sync_at).total_seconds() / 86400
    issues: list[str] = []
    if state.last_sync_status != "ok":
        issues.append(f"restore drill last run FAILED ({days_since:.1f}d ago)")
    elif days_since > stale_days:
        issues.append(
            f"restore drill stale ({days_since:.1f}d since last successful run, "
            f"threshold {stale_days}d)"
        )
    return {
        "days_since_restore_drill": round(days_since, 2),
        "last_outcome": state.last_sync_status,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _current_database_url() -> str:
    from app.config import settings

    return settings.database.url


def run_drill(
    session: Session,
    *,
    stage_dir: Path | None = None,
    database_url: str | None = None,
) -> DrillResult:
    """Run one drill end to end and persist the result. Every failure mode
    (no dump, bad dump, restore failure, mismatch) becomes a
    `DrillResult(ok=False, reason=...)`, persisted and, if configured,
    pushed — the drill's own failure must be as loud as the thing it checks.
    """
    from sqlalchemy import create_engine

    cfg = plugin_config("system")
    stage_dir = stage_dir or Path(cfg.restore_drill_stage_dir)
    database_url = database_url or _current_database_url()

    dump = find_latest_dump(stage_dir)
    if dump is None and cfg.restore_drill_remote:
        dump = pull_latest_from_remote(
            stage_dir, rclone_bin=cfg.restore_drill_rclone_bin, remote=cfg.restore_drill_remote
        )
    if dump is None:
        result = DrillResult(ok=False, reason="no dump found in staging dir or remote")
        persist_result(session, result)
        push_failure_notify(result)
        return result

    sane, reason = dump_is_sane(dump)
    if not sane:
        result = DrillResult(ok=False, dump_name=dump.name, reason=reason)
        persist_result(session, result)
        push_failure_notify(result)
        return result

    age_h = dump_age_hours(dump)

    mismatch = client_server_version_mismatch(database_url)
    if mismatch:
        result = DrillResult(ok=False, dump_name=dump.name, dump_age_hours=age_h, reason=mismatch)
        persist_result(session, result)
        push_failure_notify(result)
        return result

    drill_db = f"{SCRATCH_DB_PREFIX}{int(time.time())}"

    try:
        create_scratch_db(database_url, drill_db)
    except Exception as e:  # noqa: BLE001
        result = DrillResult(
            ok=False, dump_name=dump.name, dump_age_hours=age_h,
            reason=f"create scratch db failed: {e}",
        )
        persist_result(session, result)
        push_failure_notify(result)
        return result

    try:
        try:
            restore_dump(database_url, drill_db, dump)
        except Exception as e:  # noqa: BLE001
            result = DrillResult(
                ok=False, dump_name=dump.name, dump_age_hours=age_h,
                reason=f"pg_restore failed: {e}",
            )
            persist_result(session, result)
            push_failure_notify(result)
            return result

        live_engine = create_engine(database_url)
        drill_engine = create_engine(_with_dbname(database_url, drill_db))
        try:
            report = verify_against_live(
                live_engine,
                drill_engine,
                top_n=cfg.restore_drill_top_n_tables,
                exclude=list(getattr(cfg, 'restore_drill_volatile_tables', None) or []),
                as_of=dump_timestamp(dump),
            )
        finally:
            live_engine.dispose()
            drill_engine.dispose()

        if report["ok"]:
            reason = None
        elif report["missing_tables"]:
            reason = f"missing tables in restore: {', '.join(report['missing_tables'][:5])}"
        else:
            # Name the table(s) first: `system_alerts` shows only the first
            # 200 characters of this, and the worst ratio alone does not say
            # which table or which direction.
            reason = (
                f"row count mismatch: {'; '.join(report['failures'][:3])}"
                f" — worst ratio {report['worst_ratio']}"
            )

        result = DrillResult(
            ok=report["ok"],
            dump_name=dump.name,
            dump_age_hours=age_h,
            table_count=report["drill_table_count"],
            live_table_count=report["live_table_count"],
            worst_ratio=report["worst_ratio"],
            checked_tables=report["checked_tables"],
            reason=reason,
        )
    finally:
        drop_scratch_db(database_url, drill_db)

    persist_result(session, result)
    if not result.ok:
        push_failure_notify(result)
    return result


def main() -> int:  # pragma: no cover — thin CLI wrapper, exercised via run_drill
    logging.basicConfig(level=logging.INFO)
    from app.db import get_db

    db = get_db()
    with db.session() as session:
        result = run_drill(session)
    logger.info("restore drill: %s", json.dumps(result.to_dict()))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
