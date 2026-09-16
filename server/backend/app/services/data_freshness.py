"""Data freshness probes — detect when an integration's *output* has stalled.

Distinct from `SyncState`, which only records whether the sync *job* ran.
A job can succeed (or be a no-op like the WhatsApp embedding chunker) while
the underlying data has stopped flowing. These probes query the actual data
tables and surface "no new records in N hours" as a separate alert axis.

V4 chunk 1.3: the probed set and thresholds are no longer a hand-maintained
if/elif chain — they're driven by each integration's own manifest
(`staleness_probe`). Most probes are "MAX(timestamp_column) on model" (model
resolved from `app.models`, since every ORM class — kernel- and
integration-owned — is importable from there per chunk 1.2). A couple of
manifests describe probes that don't fit that shape (`apple_reminders` keys
off the core `User` table rather than one of its own; `homeassistant` uses
a live in-process function via `probe_function` rather than a column) — see
`app.plugin.manifest.StalenessProbe` for the schema.
"""

import importlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session

from app.plugin.manifest import StalenessProbe

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _staleness_probes() -> dict[str, StalenessProbe]:
    """{integration name: StalenessProbe} for every manifest that declares one.

    Cached for the process lifetime — manifests are static, imported once
    at startup (mirrors `services/freshness.py`'s cache).
    """
    from app.plugin.validate import discover_manifests

    return {
        name: manifest.staleness_probe
        for name, manifest in discover_manifests().items()
        if manifest.staleness_probe is not None
    }


@dataclass
class FreshnessResult:
    integration: str
    latest_ts: datetime | None
    threshold_seconds: int
    age_seconds: int | None  # None if no rows ever
    # Set only for `per_user` probes, which emit one result per owner instead of
    # one table-wide row. None means "this figure covers the whole table".
    user_id: int | None = None


def _resolve_model(model_name: str):
    """Resolve an ORM class by name from `app.models`, which re-exports both
    kernel-owned models (e.g. `User`) and every integration's declared
    models (via manifest-driven discovery, chunk 1.2)."""
    import app.models as models_module

    return getattr(models_module, model_name)


def _probe(session: Session, integration: str) -> datetime | None:
    """Return the latest-activity timestamp for `integration`'s staleness
    probe, or None if it isn't probed (no `staleness_probe` in its
    manifest) or has no rows/events yet.

    Single-integration lookup, one round trip. `check_all` doesn't call this
    for model-based probes — it batches all of them into one query via
    `_probe_all` — but this stays the entry point for anyone (tests, other
    callers) who wants a single integration's timestamp on demand.
    """
    probe = _staleness_probes().get(integration)
    if probe is None:
        return None

    if probe.probe_function:
        module_path, func_name = probe.probe_function.split(":")
        fn = getattr(importlib.import_module(module_path), func_name)
        return fn()

    model = _resolve_model(probe.model)
    column = getattr(model, probe.timestamp_column)
    query = session.query(func.max(column))
    if probe.filter_column:
        query = query.filter(getattr(model, probe.filter_column) == probe.filter_value)
    return query.scalar()


def _probe_all(session: Session, probes: dict[str, StalenessProbe]) -> dict[str, datetime | None]:
    """Resolve every probe's latest-activity timestamp with as few round
    trips as possible.

    P4 (hardening-2026-08.md): `check_all` used to run one `_probe()` call —
    one `SELECT MAX(...)` — per integration, sequentially. Every
    `model`/`timestamp_column` probe (the majority) now collapses into a
    single `UNION ALL` statement, one round trip total instead of one per
    integration. `probe_function` probes (currently just `homeassistant`'s
    WS-heartbeat check) are a live in-process call, not SQL — there's
    nothing to batch, so those still go through `_probe` individually.

    An integration missing from the returned dict (e.g. a mocked session
    that returns no rows) means "no data" — same as `_probe` returning None.
    """
    results: dict[str, datetime | None] = {}

    model_probes = {
        name: p for name, p in probes.items()
        if p.probe_function is None and not p.per_user
    }
    fn_probes = {name: p for name, p in probes.items() if p.probe_function is not None}

    if model_probes:
        selects = []
        for name, p in model_probes.items():
            model = _resolve_model(p.model)
            stmt_ = select(
                literal(name).label("integration"),
                func.max(getattr(model, p.timestamp_column)).label("latest_ts"),
            )
            # A shared-table probe (every deriver writes to algo_predictions)
            # has to narrow to its own rows, or the batched MAX reports the
            # freshest row across all of them and a dead deriver reads healthy.
            if p.filter_column:
                stmt_ = stmt_.where(getattr(model, p.filter_column) == p.filter_value)
            selects.append(stmt_)
        stmt = selects[0] if len(selects) == 1 else union_all(*selects)
        for row in session.execute(stmt).all():
            results[row.integration] = row.latest_ts

    for name in fn_probes:
        results[name] = _probe(session, name)

    return results


def _probe_per_user(
    session: Session, probes: dict[str, StalenessProbe]
) -> dict[str, dict[int, datetime]]:
    """`{integration: {user_id: latest_ts}}` for every `per_user` probe.

    One grouped query per probe rather than a single `UNION ALL`: the batched
    statement in `_probe_all` works because each branch yields exactly one row, and
    adding a `GROUP BY` breaks that shape. There are a handful of these probes, so
    a handful of round trips is the right trade for a readable query.

    ⚠️ **Owners with a NULL/absent timestamp are omitted, deliberately.** For a
    table-wide probe, no rows means the pipeline is broken and alerting is right.
    Per user it usually means that person doesn't use the integration — Sam has
    no Last.fm scrobbles and never will — and alerting on it would be permanent
    noise that trains people to ignore the panel. The cost is that a user who
    *should* have data but never has is not flagged here; that is what
    `health.coverage` and onboarding checks are for.
    """
    out: dict[str, dict[int, datetime]] = {}
    for name, probe in probes.items():
        model = _resolve_model(probe.model)
        owner = getattr(model, probe.user_column)
        ts = getattr(model, probe.timestamp_column)
        rows = session.query(owner, func.max(ts)).group_by(owner).all()
        out[name] = {
            user_id: latest
            for user_id, latest in rows
            if user_id is not None and latest is not None
        }
    return out


def _effective_threshold_minutes(name: str, probe: StalenessProbe) -> int:
    """`probe.threshold_minutes`, unless the manifest names a
    `threshold_config_key` and that key resolves to a usable override.

    2026-08-27: `apple_reminders`' 5-minute threshold made "data stale"
    indistinguishable from a MacBook asleep for the night, flapping every
    30-60 minutes around the clock (see `notifications/sweep.py`'s
    push-boundary gating for the other half of that fix). Falls back to the
    static manifest value on any problem — a bad config entry degrading one
    probe to its old, known-safe threshold beats crashing `check_all` for
    every integration.
    """
    if not probe.threshold_config_key:
        return probe.threshold_minutes
    try:
        from app.plugin.config_store import plugin_config

        value = getattr(plugin_config(name), probe.threshold_config_key)
    except Exception:  # noqa: BLE001 — config plumbing must never break freshness checks
        logger.warning(
            "could not read %s.%s override; using static threshold_minutes=%d",
            name, probe.threshold_config_key, probe.threshold_minutes,
        )
        return probe.threshold_minutes
    if not isinstance(value, int) or value <= 0:
        logger.warning(
            "ignoring bad %s.%s %r; using static threshold_minutes=%d",
            name, probe.threshold_config_key, value, probe.threshold_minutes,
        )
        return probe.threshold_minutes
    return value


def next_expected_run(after: datetime, schedule: str, schedule_timezone: str | None) -> datetime | None:
    """The next time `schedule` would fire strictly after `after`.

    Uses the exact same `CronTrigger.from_crontab(schedule, timezone=...)`
    construction as the real scheduler (`app/scheduler.py::setup_scheduler`)
    so "when it's scheduled" and "when we complain it didn't happen" can
    never drift apart. `after` may be naive (assumed UTC) or aware; the
    schedule's own `schedule_timezone` is applied by the trigger itself, so
    passing UTC in is fine regardless of what timezone the cron is defined in
    — only the instant matters for the comparison.

    Returns None if the schedule can never fire again (not expected for any
    real manifest, but the caller must treat that as "unknown, don't alarm"
    rather than crash).
    """
    from apscheduler.triggers.cron import CronTrigger

    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    trigger = CronTrigger.from_crontab(schedule, timezone=schedule_timezone)
    return trigger.get_next_fire_time(previous_fire_time=after, now=after)


def is_sync_overdue(
    last_sync_at: datetime,
    now: datetime,
    schedule: str,
    schedule_timezone: str | None,
    grace: timedelta = timedelta(minutes=2),
) -> bool:
    """Whether `schedule` implies a run was due strictly between
    `last_sync_at` and `now`.

    This is the fix for the "commute alarms every afternoon" bug: a flat
    staleness cutoff has no idea that `commute`'s cron only fires in a
    07:00-08:57 weekday window, so it alarms every minute outside that
    window forever. Asking "did the integration's own cron schedule expect
    another run since it last succeeded" is correct for both shapes —
    continuous (`*/15 * * * *`) and windowed (`0-57 7-8 * * 1-5`) — with no
    per-integration special case.

    `grace` absorbs the gap between "a tick became due" and "the scheduler
    actually got around to running it" (matches the spirit of
    `misfire_grace_time` elsewhere in this codebase) — without it, a job
    would appear "overdue" for the same instant it becomes eligible to run,
    before it's had any chance to.
    """
    expected = next_expected_run(last_sync_at, schedule, schedule_timezone)
    if expected is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return expected + grace <= now


def _commute_window_active(now_utc: datetime) -> bool:
    """Whether the scheduled commute solve could plausibly be running right
    now (weekday 07:00-08:57 Europe/Dublin — CommuteIntegration.sync_schedule).
    Outside it, "no fresh commute row" is expected, not a data problem, so
    `check_all` must not evaluate the probe at all rather than emitting a
    misleading age/None."""
    from zoneinfo import ZoneInfo

    dublin = now_utc.astimezone(ZoneInfo("Europe/Dublin"))
    return dublin.weekday() < 5 and (7, 0) <= (dublin.hour, dublin.minute) <= (8, 57)


def unmeasured_integrations() -> list[str]:
    """Names of every integration package with no staleness probe at all.

    `check_all` only ever iterates `_staleness_probes()`, so an integration
    with no `staleness_probe` in its manifest is simply absent from its
    output — there is nothing there to distinguish "measured and healthy"
    from "never instrumented". 19 of the 26 packages are in this state
    (finance, google_calendar, historical_corpus, coffee, attachments,
    inbox, obsidian, snags, household, embedding, notifications, ...).

    Deliberately does NOT include `commute` even during the hours its
    window-gate (`_commute_window_active`) excludes it from `check_all`'s
    result — that integration *is* probed, just not evaluated right now,
    which is a different state from never having been instrumented at all.
    Callers that want to distinguish "out of window" from "measured" should
    use `check_all`'s absence of a `commute` row alongside this list, which
    never mentions it.
    """
    from app.plugin.validate import discover_manifests

    probed = set(_staleness_probes())
    return sorted(name for name in discover_manifests() if name not in probed)


def check_all(session: Session) -> list[FreshnessResult]:
    """Run every freshness probe. Returns a result per integration whose
    probe is currently meaningful — the caller decides whether to alert
    based on `age_seconds > threshold_seconds`. `commute` is window-gated:
    it's only included while the weekday-morning solve window is active
    (see `_commute_window_active`), so it never alarms the rest of the day.
    """
    now = datetime.now(timezone.utc)
    results: list[FreshnessResult] = []

    active_probes = {
        name: probe
        for name, probe in _staleness_probes().items()
        if name != "commute" or _commute_window_active(now)
    }
    per_user_probes = {n: p for n, p in active_probes.items() if p.per_user}
    table_probes = {n: p for n, p in active_probes.items() if not p.per_user}

    latest_by_name = _probe_all(session, table_probes)
    latest_by_user = _probe_per_user(session, per_user_probes) if per_user_probes else {}

    def _age(latest: datetime | None) -> tuple[datetime | None, int | None]:
        if latest is None:
            return None, None
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        return latest, int((now - latest).total_seconds())

    for name, probe in sorted(active_probes.items()):
        if probe.per_user:
            # One result per owner, so a single laggard is visible instead of
            # being hidden behind whoever is healthiest.
            for user_id, latest_raw in sorted(latest_by_user.get(name, {}).items()):
                latest, age = _age(latest_raw)
                results.append(
                    FreshnessResult(
                        integration=name,
                        latest_ts=latest,
                        threshold_seconds=_effective_threshold_minutes(name, probe) * 60,
                        age_seconds=age,
                        user_id=user_id,
                    )
                )
            continue

        latest, age = _age(latest_by_name.get(name))
        results.append(
            FreshnessResult(
                integration=name,
                latest_ts=latest,
                threshold_seconds=_effective_threshold_minutes(name, probe) * 60,
                age_seconds=age,
            )
        )
    return results


def format_age(seconds: int) -> str:
    """Compact human age string for alert messages."""
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d {h}h" if h else f"{d}d"
