"""MCP tool definitions for system-level diagnostics and composite routines.

`system` is the one package whose entire point is composing other
integrations' data (morning briefing, week ahead, search everything). Before
V4 chunk 4.2 it did that with raw `from app.integrations.<other>.tools import
handle_*` imports — reaching straight into another package's internals with
no declared contract. Every cross-integration call below now goes through
`app.plugin.capabilities.get_capability(name)`, resolving a capability
string (declared in this package's own `manifest.py::depends_on`, and in
each provider's `manifest.py::provides`) to that provider's `facade.py`
object — see that module's docstring for the registry mechanism, and each
`app.integrations.<x>.facade` module for what's actually exposed. Note this
module doesn't import `app.integrations.<other>` *at all* anymore — the only
import needed is the capability lookup itself.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import Integer, func
from sqlalchemy.orm import Session

from app.mixins import UserOwnedMixin
from app.models.clients import ClientToken
from app.models.tokens import OAuthToken, SyncState
from app.models.runs import Run
from app.integrations import get_all
from app.integrations.system import device_fleet
from app.integrations.system import host_fleet
from app.integrations.system.contradictions import handle_contradictions
from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config
from app.tools import CustomTool, ToolAnnotations

# All four tools here are read-only diagnostics/composites — shared constant
# so the DSL conversion (V4 chunk 4.3e) doesn't have to repeat the identical
# inline dict four times (matches the shared `_READ_ONLY` pattern used
# elsewhere, e.g. apple_health/homeassistant's tools.py).
_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

# Thresholds for the tool-call health axis (system_alerts axis 4).
TOOL_FAILURE_THRESHOLD = 3       # >3 error rows for one tool in the lookback window
TOOL_FAILURE_LOOKBACK_MINUTES = 60
TOOL_P95_LOOKBACK_HOURS = 24
TOOL_P95_MIN_CALLS = 10           # don't judge p95 off a handful of calls
TOOL_P95_THRESHOLD_MS = 5000

# Thresholds for the daemon liveness/task-health axis (system_alerts axis 5,
# F11b). Emergency fallback only, used when `system.daemon_silent_minutes`
# (config_schema, see manifest.py) resolves to something unusable — see
# `_daemon_silent_minutes()` below for the live value. Was a flat 20 minutes
# (4 missed 5-minute heartbeats) until 2026-08-27: a MacBook with the lid
# closed overnight is indistinguishable from a dead daemon at 20 minutes, and
# it flapped `macbook:daemon_silent` every 30-60 minutes around the clock —
# see notifications/sweep.py's push-boundary gating for the other half of
# that fix.
DAEMON_SILENT_MINUTES = 20
DAEMON_TASK_RESTART_THRESHOLD = 3

# Thresholds for the index-rebuild axis (system_alerts axis 9, Wave 5.2).
# Emergency fallback only, used when `system.index_rebuild_pending_threshold`
# resolves to something unusable — see `_index_rebuild_pending_threshold()`.
INDEX_REBUILD_PENDING_THRESHOLD = 50
# `app/scripts/reembed.py`'s `record_run` name — see `_backfill_run_in_progress()`.
EMBEDDING_BACKFILL_RUN_NAME = "embedding_reembed"
# A `runs` row that's open (no `finished_at`) past this age is treated as an
# abandoned/crashed process, not a live backfill — otherwise a killed script
# would pin `rebuilding: true` forever (nothing else ever closes that row).
EMBEDDING_BACKFILL_MAX_AGE_HOURS = 24

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Composite tool handlers — aggregate data from multiple integrations
# ---------------------------------------------------------------------------


def handle_morning_briefing(session: Session, arguments: dict[str, Any]) -> str:
    """Aggregate morning overview: calendar, weather, reminders, unread email, health."""
    calendar = get_capability("calendar.query")
    weather = get_capability("weather.query")
    reminders = get_capability("reminders.query")
    mail = get_capability("mail.query")
    health = get_capability("health.query")
    home = get_capability("homeassistant.entities")
    commute = get_capability("commute.query")

    sections = {}

    # Calendar
    try:
        sections["calendar"] = json.loads(calendar.today(session, {}))
    except Exception as e:
        sections["calendar"] = {"error": str(e)}

    # Weather
    try:
        sections["weather_current"] = json.loads(weather.current(session, {}))
    except Exception as e:
        sections["weather_current"] = {"error": str(e)}

    try:
        sections["weather_forecast"] = json.loads(weather.forecast(session, {"days": 1}))
    except Exception as e:
        sections["weather_forecast"] = {"error": str(e)}

    # Reminders (incomplete)
    try:
        sections["reminders"] = json.loads(reminders.list_reminders(session, {}))
    except Exception as e:
        sections["reminders"] = {"error": str(e)}

    # Unread email
    try:
        sections["unread_email"] = json.loads(mail.unread(session, {"limit": 10}))
    except Exception as e:
        sections["unread_email"] = {"error": str(e)}

    # Health
    try:
        sections["health"] = json.loads(health.summary(session, {}))
    except Exception as e:
        sections["health"] = {"error": str(e)}

    # Home (Home Assistant status snapshot)
    try:
        sections["home"] = json.loads(home.home_status(session, {}))
    except Exception as e:
        sections["home"] = {"error": str(e)}

    # Commute — only included when there's a fresh decision (weekday mornings).
    try:
        decision = commute.recent_decision(session)
        if decision is not None:
            sections["commute"] = decision
    except Exception as e:
        sections["commute"] = {"error": str(e)}

    # System alerts
    try:
        sections["alerts"] = json.loads(handle_alerts(session, {}))
    except Exception as e:
        sections["alerts"] = {"error": str(e)}

    return json.dumps(sections, indent=2)


def handle_daily_brief(session: Session, arguments: dict[str, Any]) -> str:
    """Every read-only source the daily note needs, composed in one call.

    Supersedes `system_morning_briefing` for the `/daily-note` and `/checkin`
    commands: same idea, but it covers all 23 read-only sources instead of 8,
    fans out in parallel, honours the caller's preferences, and caches the
    non-volatile half. See `app.integrations.system.brief` for the design.
    """
    from app.auth.context import current_user_id
    from app.integrations.system import brief

    sections = arguments.get("sections")
    if isinstance(sections, str):  # tolerate "email,transport"
        sections = [s for s in sections.split(",") if s.strip()]

    payload = brief.build(
        session,
        current_user_id(),
        since=(arguments.get("since") or None),
        sections=sections,
        refresh=bool(arguments.get("refresh", False)),
        render=bool(arguments.get("render", False)),
    )
    # Compact on purpose: indent=2 added 27k characters (130k → 103k measured
    # 2026-09-02) to a payload a model reads, not a person.
    return json.dumps(payload, default=str)


def handle_conversations_since(session: Session, arguments: dict[str, Any]) -> str:
    """Group WhatsApp/Gmail messages received since `since` into
    conversations, deterministically — see `app.integrations.system.
    conversations` for the grouping rule (WhatsApp: chat + time-gap burst;
    Gmail: native thread_id) and what's excluded (from-me-only groups, the
    WhatsApp self-notes channel). Built for `/kickoff`'s triage step
    (lios#192) so messages are reviewed in context rather than one at a
    time; any caller wanting thread-aware reading can use it the same way.
    """
    from app.auth.context import current_user_id
    from app.integrations.system import conversations
    from app.tools.base import parse_iso_date

    since_arg = arguments.get("since")
    since = parse_iso_date(since_arg) if since_arg else None
    if since is None:
        raise ValueError(f"'since' is required and must be a valid ISO timestamp (got {since_arg!r})")

    now = datetime.now(timezone.utc)
    if since < now - timedelta(days=conversations.MAX_SINCE_DAYS):
        raise ValueError(
            f"'since' cannot be more than {conversations.MAX_SINCE_DAYS} days ago (got {since.isoformat()})"
        )

    until_arg = arguments.get("until")
    until = parse_iso_date(until_arg) if until_arg else None
    if until_arg and until is None:
        raise ValueError(f"invalid 'until': {until_arg!r}")

    sources = arguments.get("sources") or None
    limit = int(arguments.get("limit", conversations.DEFAULT_LIMIT))

    payload = conversations.conversations_since(
        session,
        current_user_id(),
        since,
        until,
        sources=sources,
        limit=limit,
    )
    return json.dumps(payload, default=str)


def handle_week_ahead(session: Session, arguments: dict[str, Any]) -> str:
    """Aggregate week overview: 7-day calendar, reminders, forecast."""
    calendar = get_capability("calendar.query")
    reminders = get_capability("reminders.query")
    weather = get_capability("weather.query")

    sections = {}

    # Calendar (next 7 days)
    try:
        sections["calendar"] = json.loads(calendar.list_events(session, {"days": 7}))
    except Exception as e:
        sections["calendar"] = {"error": str(e)}

    # Reminders (all incomplete)
    try:
        sections["reminders"] = json.loads(reminders.list_reminders(session, {}))
    except Exception as e:
        sections["reminders"] = {"error": str(e)}

    # Weather forecast (7 days)
    try:
        sections["forecast"] = json.loads(weather.forecast(session, {"days": 7}))
    except Exception as e:
        sections["forecast"] = {"error": str(e)}

    return json.dumps(sections, indent=2)


def handle_search_everything(session: Session, arguments: dict[str, Any]) -> str:
    """Unified search across vault, email, and WhatsApp using embeddings."""
    query = arguments.get("query", "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    limit = min(int(arguments.get("limit", 5)), 20)

    vault = get_capability("vault.query")
    mail = get_capability("mail.query")
    whatsapp = get_capability("whatsapp.query")

    sections = {}

    # Vault semantic search
    try:
        sections["vault"] = json.loads(vault.search(session, {"query": query, "limit": limit}))
    except Exception as e:
        sections["vault"] = {"error": str(e)}

    # Gmail semantic search
    try:
        sections["email"] = json.loads(mail.semantic_search(session, {"query": query, "limit": limit}))
    except Exception as e:
        sections["email"] = {"error": str(e)}

    # WhatsApp semantic search
    try:
        sections["whatsapp"] = json.loads(whatsapp.semantic_search(session, {"query": query, "limit": limit}))
    except Exception as e:
        sections["whatsapp"] = {"error": str(e)}

    return json.dumps(sections, indent=2)

# Integrations without a sync schedule shouldn't trigger staleness alerts
# (they're on-demand only, e.g. irish_rail, finance; or push-fed, e.g.
# apple_health, apple_reminders). Returns manifests (not just names) because
# axis 1 below needs each one's `schedule`/`schedule_timezone` to know
# *when* a sync is actually due, not just whether one exists.
def _daemon_silent_minutes() -> int:
    """How long a daemon token may go quiet before "daemon silent" fires.

    Config-driven (`system.daemon_silent_minutes`) since 2026-08-27 — see
    `DAEMON_SILENT_MINUTES`'s comment above for why. Falls back to that
    historical constant on any config problem: a mis-set value degrading
    detection to the old noisy-but-safe behaviour beats crashing this whole
    alerts axis.
    """
    value = plugin_config("system").daemon_silent_minutes
    if not isinstance(value, int) or value <= 0:
        logger.warning(
            "ignoring bad system.daemon_silent_minutes %r; using default %d",
            value, DAEMON_SILENT_MINUTES,
        )
        return DAEMON_SILENT_MINUTES
    return value


def _user_label(session, user_id: int) -> str:
    """Display name for `user_id`, falling back to `user <id>`.

    Cheap and best-effort: an alert that says "user 2" is still actionable, so a
    missing row must never cost the alert.
    """
    from app.models.users import User

    try:
        user = session.query(User).filter_by(id=user_id).first()
        return (user.display_name or user.name) if user else f"user {user_id}"
    except Exception:  # noqa: BLE001
        return f"user {user_id}"


def _scheduled_integrations() -> dict[str, Any]:
    from app.plugin.validate import discover_manifests

    manifests = discover_manifests()
    return {
        name: manifest
        for name in get_all()
        if (manifest := manifests.get(name)) is not None and manifest.schedule
    }


# ---------------------------------------------------------------------------
# Per-user vs household classification (F-alerts-scoping)
#
# `system_alerts` has two consumers with opposite requirements: the per-user
# daily brief/briefing (should show only what's relevant to the caller) and
# the notifications sweep + dashboard (household infrastructure — must see
# everything, always, cron-driven with no user pinned). The classification
# below is *derived*, never hand-listed: an integration is "per-user" iff at
# least one of the models it declares in its own `manifest.py::models` is
# `UserOwnedMixin` — the exact same authority `app/mixins.py` documents and
# `tests/test_user_scoping.py` already sweeps every such model against. A
# household-shared integration (weather, homeassistant, commute, finance,
# snags, ...) has no such model by construction and is therefore never
# suppressed for anyone.
# ---------------------------------------------------------------------------


def _per_user_integration_names() -> set[str]:
    """Integration names that own at least one `UserOwnedMixin` model."""
    import importlib

    from app.plugin.validate import discover_manifests

    names: set[str] = set()
    for name, manifest in discover_manifests().items():
        if not manifest.models:
            continue
        try:
            models_module = importlib.import_module(f"app.integrations.{name}.models")
        except ModuleNotFoundError:
            continue
        for model_name in manifest.models:
            cls = getattr(models_module, model_name, None)
            if isinstance(cls, type) and issubclass(cls, UserOwnedMixin):
                names.add(name)
                break
    return names


def _user_has_data_for_integration(session: Session, name: str, user_id: int) -> bool:
    """Cheap presence check across a per-user integration's own tables.

    Deliberately generic (a plain `COUNT(*) WHERE user_id = :user_id` over
    every `UserOwnedMixin` model the integration declares) rather than a
    per-integration `has_data()` call — several integrations that qualify as
    per-user here (e.g. `apple_reminders`, `obsidian`) don't expose a
    `has_data()` on their facade at all, and this needs no manifest/facade
    change to work for all of them uniformly.
    """
    import importlib

    from app.plugin.validate import discover_manifests

    manifest = discover_manifests().get(name)
    if manifest is None or not manifest.models:
        return False
    try:
        models_module = importlib.import_module(f"app.integrations.{name}.models")
    except ModuleNotFoundError:
        return False
    for model_name in manifest.models:
        cls = getattr(models_module, model_name, None)
        if not isinstance(cls, type) or not issubclass(cls, UserOwnedMixin):
            continue
        try:
            count = session.query(func.count()).select_from(cls).filter(
                cls.user_id == user_id
            ).scalar()
        except Exception:  # noqa: BLE001
            # A broken presence check must never hide a genuine alert —
            # unknown is treated as "has data" (fail open, not silent).
            logger.exception("alerts: has-data check failed for %s/%s", name, model_name)
            return True
        if count:
            return True
    return False


DISK_WARN_PERCENT = 85


def _disk_issue(path: str = "/", warn_percent: int = DISK_WARN_PERCENT) -> str | None:
    """A sentence when the filesystem under `path` is nearly full, else None.

    Says what will happen, not just the number: the reader of `system_alerts`
    is deciding whether to act now, and "92 % used" is a fact while "Postgres
    will crash at 100 %" is a reason.
    """
    import shutil

    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    pct = round(usage.used * 100 / usage.total)
    if pct < warn_percent:
        return None
    free_gb = usage.free / 1e9
    return (
        f"disk {pct}% used ({free_gb:.1f} GB free) — image pulls will fail and "
        f"Postgres crashes at 100%; prune images (`docker image prune -af`)"
    )


def _index_rebuild_pending_threshold() -> int:
    """How many `queue_pending` rows count as an actual rebuild.

    Config-driven (`system.index_rebuild_pending_threshold`) since Wave 5.2
    — the steady-state queue holds ~3 rows at all times (the 5-minute
    embedding worker is always slightly behind), so a flat `pending > 0`
    reported "rebuilding" continuously in production with no rebuild
    running. Falls back to the historical constant on any config problem,
    same pattern as `_daemon_silent_minutes()`.
    """
    value = getattr(plugin_config("system"), "index_rebuild_pending_threshold", None)
    if not isinstance(value, int) or value <= 0:
        logger.warning(
            "ignoring bad system.index_rebuild_pending_threshold %r; using default %d",
            value, INDEX_REBUILD_PENDING_THRESHOLD,
        )
        return INDEX_REBUILD_PENDING_THRESHOLD
    return value


def _backfill_run_in_progress(session: Session) -> bool:
    """Is `app/scripts/reembed.py` (name `embedding_reembed`) currently mid-run?

    Needed as an OR alongside the pending-count threshold because
    `backfill.fill_space()` bypasses `EmbeddingQueue` entirely — it calls the
    provider and writes vector rows directly, in-process, often for hours —
    so a `fill-space` run would otherwise report `rebuilding: false` for its
    whole duration despite genuinely being a rebuild. `reclean()` does queue
    through `EmbeddingQueue`, so the threshold check catches that case too,
    but checking here is still correct and cheap for it.

    An open row (`finished_at IS NULL`) older than
    `EMBEDDING_BACKFILL_MAX_AGE_HOURS` is treated as abandoned, not running
    — otherwise a killed process pins this true forever, since nothing else
    ever closes that row.
    """
    from app.models.runs import Run

    cutoff = datetime.now(timezone.utc) - timedelta(hours=EMBEDDING_BACKFILL_MAX_AGE_HOURS)
    row = (
        session.query(Run.id)
        .filter(
            Run.name == EMBEDDING_BACKFILL_RUN_NAME,
            Run.finished_at.is_(None),
            Run.started_at >= cutoff,
        )
        .first()
    )
    return row is not None


def _index_state(session: Session) -> dict:
    """R4 (2026-09-04); rebuild rule fixed Wave 5.2 (2026-09-05).

    `rebuilding` is true when EITHER (a) `queue_pending` exceeds
    `system.index_rebuild_pending_threshold` (default 50) — the steady-state
    queue sits around ~3 rows, so a flat `pending > 0` was reporting a
    rebuild that wasn't happening — OR (b) the `embedding_reembed` backfill
    script (`app/scripts/reembed.py`) has an open `runs` row, since its
    `fill-space` command bypasses the queue and would otherwise leave no
    signal at all. `rebuild_reason` names which one fired
    (`"pending_above_threshold"` / `"backfill_running"`), or `None` when
    neither did, so a caller doesn't have to re-derive it.

    `percent_complete` estimates how much of the target corpus is actually
    embedded — `already-embedded / (already-embedded + still-queued)` — so a
    caller can tell "index is fine" from "index is 40% through a backfill"
    without a raw psql query. Cross-source deliberately: vault, email,
    whatsapp, historical_corpus, coffee and tasks all share one queue and one
    worker, and the question this answers ("can I trust a search result's
    completeness right now") doesn't split by producer.

    `queue_errors` surfaces separately from `queue_pending` because a poison
    item that gave up after MAX_EMBED_ATTEMPTS is not "still working through
    it" — see `EmbeddingService._embed_and_store`'s bisection/give-up path.

    Imports `app.services.embedding` (a kernel service module), never
    `app.integrations.embedding` directly — this package composes other
    integrations' data through capabilities/facades per the module docstring
    above, and `EmbeddingQueue`/`Embedding` are re-exported there for exactly
    this kind of kernel-level read. `CHUNKER_VERSION` (obsidian-specific, and
    genuinely `app.integrations.obsidian` internals) is deliberately not
    surfaced here for the same reason — this axis is cross-source and a
    single source's chunker version isn't. `app.models.runs.Run` is a
    kernel-owned table (not another integration's internals), same as
    `ClientToken`/`OAuthToken` above, so it's imported directly too.
    """
    from app.services.embedding import Embedding, EmbeddingQueue

    embedded_total = session.query(func.count(Embedding.id)).scalar() or 0
    pending = (
        session.query(func.count(EmbeddingQueue.id))
        .filter(EmbeddingQueue.status.in_(("pending", "processing")))
        .scalar() or 0
    )
    errors = (
        session.query(func.count(EmbeddingQueue.id))
        .filter(EmbeddingQueue.status == "error")
        .scalar() or 0
    )
    denom = embedded_total + pending
    percent_complete = round(100.0 * embedded_total / denom, 1) if denom else 100.0

    rebuild_reason = None
    if pending > _index_rebuild_pending_threshold():
        rebuild_reason = "pending_above_threshold"
    elif _backfill_run_in_progress(session):
        rebuild_reason = "backfill_running"

    return {
        "rebuilding": rebuild_reason is not None,
        "rebuild_reason": rebuild_reason,
        "percent_complete": percent_complete,
        "queue_pending": pending,
        "queue_errors": errors,
    }


def handle_alerts(session: Session, arguments: dict[str, Any]) -> str:
    """Return integrations that are failing, stale, or producing no data.

    Two consumers, opposite requirements:

    - The per-user daily brief / `system_alerts` MCP tool: should show only
      what's relevant to *this* caller. Sam shouldn't see a stale commute
      sync she never uses, or Alex's Apple Health gap.
    - The notifications sweep (`app.integrations.notifications.sweep`, a
      household-wide `*/15` cron with no user pinned) and the dashboard route
      (`app/routes/system.py`): must keep seeing *everything* regardless of
      who, if anyone, happens to be asking — a dead HA event stream is a real
      fault someone has to know about no matter whose request triggered the
      check.

    This function resolves the caller's scope exactly once, from the
    ambient `current_user_id_or_none()` ContextVar — the same mechanism every
    other per-user tool handler in this codebase reads (see
    `app/auth/context.py`). That's safe here because both consumers that need
    the household view genuinely run with nothing bound: the sweep is a
    background asyncio task, and the dashboard route uses cookie auth, not a
    per-user bearer, so nothing calls `use_user()` on that path today. For the
    one caller where "nothing bound" absolutely must not depend on that
    happening to remain true, see `handle_alerts_household` below — an
    explicit, context-independent entry point, which is what the sweep and
    the dashboard route actually call.

    Three independent freshness/health axes, further split per-user vs
    household by `_per_user_integration_names()` (derived from
    `UserOwnedMixin`, never hand-listed — see that function's docstring):
    - SyncState: did the sync job run successfully and recently?
    - Data freshness: is new data actually landing in the table?
    - Apple Health coverage gaps, OAuth re-auth, and daemon liveness are all
      attributable to a specific user_id at the row level, so those are
      filtered to the caller directly rather than via the has-data check.

    The first two axes are independent — a job can succeed while data flow
    has stopped (e.g. when a job is just an embedding chunker, not the
    producer).
    """
    from app.auth.context import current_user_id_or_none

    return _render_alerts(session, arguments, scope_user_id=current_user_id_or_none())


def handle_alerts_household(session: Session, arguments: dict[str, Any]) -> str:
    """The unfiltered, whole-fleet view — for callers that must never scope

    down by accident. Unlike `handle_alerts`, this never reads the ambient
    user context at all: the notifications sweep (cron, no request, no
    binding possible) and the dashboard route (cookie auth, no per-user
    binding today) call this explicitly instead of relying on "nothing
    happens to be bound right now" staying true forever.
    """
    return _render_alerts(session, arguments, scope_user_id=None)


def _render_alerts(
    session: Session, arguments: dict[str, Any], *, scope_user_id: int | None
) -> str:
    payload = _build_alerts_payload(session, arguments, scope_user_id=scope_user_id)
    alerts = payload["alerts"]
    reauth_needed = payload["reauth_needed"]
    tool_alerts = payload["tool_alerts"]
    return json.dumps(payload, indent=2 if alerts or reauth_needed or tool_alerts else None)


def _build_alerts_payload(
    session: Session, arguments: dict[str, Any], *, scope_user_id: int | None
) -> dict:
    """Compute the alerts payload, optionally scoped to one user.

    `scope_user_id=None` is the household view (every integration, every
    user's issues) — what the sweep and dashboard need. An int scopes the
    result to that caller: per-user integrations the caller has no data for
    are dropped entirely, and issues already attributable to a specific
    user_id (health coverage gaps, OAuth re-auth) are filtered to just theirs.
    """
    from app.services.data_freshness import (
        check_all as check_freshness,
        format_age,
        is_sync_overdue,
        unmeasured_integrations,
    )

    threshold_minutes = int(arguments.get("threshold_minutes", 60))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
    scheduled = _scheduled_integrations()

    states = {s.integration: s for s in session.query(SyncState).all()}
    alerts_by_integration: dict[str, dict] = {}

    def _alert(name: str) -> dict:
        if name not in alerts_by_integration:
            state = states.get(name)
            entry = {
                "integration": name,
                "status": state.last_sync_status if state else "unknown",
                "issues": [],
                "consecutive_failures": (state.consecutive_failures or 0) if state else 0,
            }
            if state and state.last_error:
                entry["last_error"] = state.last_error[:200]
            if state and state.last_sync_at:
                entry["last_sync_at"] = state.last_sync_at.isoformat()
            alerts_by_integration[name] = entry
        return alerts_by_integration[name]

    def _attribute(name: str, issue: str, user_id: int | None) -> None:
        """Record, *structurally*, that `issue` belongs to one household member.

        The rendered issue string already names them ("data stale for Sam"),
        but a consumer that has to regex a human name out of prose is a
        consumer that breaks when the wording changes — `notifications`'
        `_USER_ATTRIBUTED_ISSUE` is exactly that, and it only ever covered the
        one health-coverage shape. `issue_users` is keyed on the issue string
        (not its list index) so the two lists can never drift out of
        alignment, and it is only present on entries that actually have an
        attributable issue.

        Per-*issue* rather than per-entry because one integration's entry can
        collect issues owned by different people: a `per_user` staleness probe
        emits one row per owner, and both land under the same integration name.
        """
        if user_id is None:
            return
        _alert(name).setdefault("issue_users", {})[issue] = user_id

    # Axis 1: SyncState. "Stale" means both (a) longer than the caller's flat
    # `threshold_minutes` since the last successful sync, *and* (b) the
    # integration's own cron schedule implies a run was actually due in that
    # gap (`is_sync_overdue`) — condition (a) alone is what made `commute`
    # (a weekday-morning-only window: "0-57 7-8 * * 1-5") alarm every
    # afternoon and all weekend: it was always ">60 minutes since last
    # sync" outside its window, which is expected, not a fault. Condition
    # (b) alone would be too eager to re-alarm the instant a job becomes
    # due before it's had a chance to run, hence keeping (a) as well.
    now = datetime.now(timezone.utc)
    for state in states.values():
        issues = []
        if state.consecutive_failures and state.consecutive_failures >= 1:
            issues.append(f"failing ({state.consecutive_failures}x consecutive)")
        manifest = scheduled.get(state.integration)
        if (
            manifest is not None
            and state.last_sync_at
            and state.last_sync_at < cutoff
            and is_sync_overdue(
                state.last_sync_at, now, manifest.schedule, manifest.schedule_timezone
            )
        ):
            age_mins = int((now - state.last_sync_at).total_seconds() / 60)
            if age_mins >= 60:
                issues.append(f"sync stale (last sync {age_mins // 60}h {age_mins % 60}m ago)")
            else:
                issues.append(f"sync stale (last sync {age_mins}m ago)")
        if state.last_sync_status == "never":
            issues.append("never synced")
        if issues:
            _alert(state.integration)["issues"].extend(issues)

    # Axis 2: data freshness — populate per-integration ages, alert when stale
    freshness = []
    for r in check_freshness(session):
        # A `per_user` probe emits one result per owner (see
        # `data_freshness._probe_per_user`), so the owner has to be carried into
        # both the panel row and the alert text — "reminders data is stale" is not
        # actionable when two people's Macs feed it and only one has stopped.
        # A per-owner row belongs to one person, so a scoped caller must not see
        # somebody else's stalled daemon — the same rule this function already
        # applies to health coverage gaps and OAuth re-auth, and the reason its
        # docstring lists "daemon liveness" among the row-level attributable axes.
        # The household view (scope_user_id=None — the notifications sweep and the
        # dashboard) sees every owner, which is the point.
        if scope_user_id is not None and r.user_id is not None and r.user_id != scope_user_id:
            continue

        who = f" for {_user_label(session, r.user_id)}" if r.user_id is not None else ""
        entry = {
            "integration": r.integration,
            "latest": r.latest_ts.isoformat() if r.latest_ts else None,
            "age": format_age(r.age_seconds) if r.age_seconds is not None else None,
            "threshold": format_age(r.threshold_seconds),
        }
        if r.user_id is not None:
            entry["user_id"] = r.user_id
        freshness.append(entry)
        if r.age_seconds is None:
            issue = f"data stale{who} (no records in table)"
        elif r.age_seconds > r.threshold_seconds:
            issue = (
                f"data stale{who} (latest record {format_age(r.age_seconds)} old, "
                f"threshold {format_age(r.threshold_seconds)})"
            )
        else:
            continue

        # "Stale" has two causes with different owners, and rendering them
        # identically sends every investigation to the wrong place.
        #
        # Measured on `lastfm`, 2026-08-19: SyncState `ok`, 0 consecutive
        # failures, last run 21:30 — and the Last.fm API itself returned the
        # exact same most-recent play as our own table (17 Aug 15:26). comar was
        # perfectly in sync; the *source* had stopped producing, because the
        # Scrobbler app quit submitting. The alert nonetheless read "lastfm: data
        # stale", which is indistinguishable from a broken sync, so the same
        # alert was investigated as a comar fault on 13 Aug and again tonight.
        #
        # A sync that is succeeding and returning nothing is not a fault in this
        # system. Say so, and point at the thing that actually stopped.
        # ⚠️ Only meaningful for an integration that actually *pulls on a
        # schedule*. Caught in live verification 2026-08-19: `apple_reminders`
        # has `schedule=None` (the Mac daemon pushes on its own timer), so its
        # SyncState is not evidence of a healthy fetch — there is no fetch. The
        # first deploy of this check rendered
        #
        #   "data stale for Sam ... — sync is healthy, so there is no new data
        #    at the source"
        #
        # when the truth was the exact opposite: her daemon was dead and data was
        # being *lost*, not absent. "The sync ran fine and found nothing" can only
        # be said by something that ran a sync.
        state = states.get(r.integration)
        upstream_dry = (
            state is not None
            and r.integration in scheduled
            and state.last_sync_status == "ok"
            and not (state.consecutive_failures or 0)
            and r.age_seconds is not None
        )
        if upstream_dry:
            # Wording matters: "no new data at the source", not "the source has
            # stopped". A batching upstream (the Last.fm scrobbler flushes days of
            # plays at once) is not broken, and calling it stopped sends someone
            # to restart a thing that was about to catch up on its own.
            issue += " — sync is healthy, so there is no new data at the source"

        _alert(r.integration)["issues"].append(issue)
        _attribute(r.integration, issue, r.user_id)

    # Axis 2b: health data *coverage*. Axis 2 asks "did anything land recently";
    # this asks "is any day missing". They diverge precisely in the case we care
    # about: Health Auto Export re-sends a trailing window, so a push carrying
    # only old rows keeps the staleness probe green while a hole from an
    # off-network stretch sits unrepaired in the middle of the data. Scoped
    # per-user, unlike the kernel probe's table-wide MAX().
    try:
        gaps = get_capability("health.coverage").coverage_gaps(session)
    except Exception:  # noqa: BLE001
        # Never let one axis take down the whole alerts payload — the dashboard
        # banner and the notifications cron both render from it.
        logger.exception("health coverage check failed")
        gaps = []
    for gap in gaps:
        # A gap is attributed to a specific user_id — when scoped, a gap in
        # the *other* user's health data must never appear here (that's the
        # exact bug: Sam being shown Alex's Apple Health hole). It still
        # reaches the household view (scope_user_id=None) unfiltered, so the
        # notifications sweep still alerts on it regardless of who asked.
        if scope_user_id is not None and gap["user_id"] != scope_user_id:
            continue
        missing = gap["missing_days"]
        shown = ", ".join(missing[:3]) + (f" +{len(missing) - 3} more" if len(missing) > 3 else "")
        issue = (
            f"data gap for user {gap['user_id']}: {len(missing)} of last "
            f"{gap['checked_days']} days missing ({shown})"
        )
        _alert("apple_health")["issues"].append(issue)
        _attribute("apple_health", issue, gap["user_id"])

    # Axis 2c: the source has gone quiet. Axes 2 and 2b both reason about data
    # comar *received*, so both stay silent for as long as their thresholds take
    # to expire - 36h for staleness, because real export gaps run 12-20h. This
    # one reads the last push *attempt* instead, so a phone that has stopped
    # calling shows up while the answer is still "check Tailscale", not "a day
    # and a half of health data is missing".
    #
    # Household-scoped, not per-user: SyncState has one row per integration, so
    # it cannot say *whose* phone went quiet. Attributing it to a user would be
    # a guess, and mis-attribution is what the `issue_users` work fixed.
    try:
        silence = get_capability("health.coverage").push_silence(session)
    except Exception:  # noqa: BLE001
        logger.exception("health push-silence check failed")
        silence = None
    if silence:
        _alert("apple_health")["issues"].append(
            f"no push attempt in {silence['hours_silent']}h "
            f"(threshold {silence['threshold_hours']}h, last attempt "
            f"{silence['last_attempt']}, status {silence['last_status']}) "
            f"- the iOS app has stopped calling; check Health Auto Export "
            f"and that Tailscale is on"
        )

    # Axis 3: OAuth re-auth required. A flagged token means the integration's
    # sync is permanently failing until the user completes a fresh consent flow
    # — distinct from a transient sync error. Surface as its own block so the
    # daily-note briefing and dashboard banner can render a one-click recovery
    # link instead of generic "21x consecutive" noise.
    from app.auth.oauth import google_login_url
    from app.models.users import User

    reauth_needed = [
        {
            "provider": t.provider,
            "account_email": t.account_email,
            "user_id": t.user_id,
            "flagged_at": t.needs_reauth_at.isoformat() if t.needs_reauth_at else None,
            "reason": t.needs_reauth_reason,
            # Carries the signed `start` the session-exempt login route
            # requires (app/auth/oauth.py::google_login_url). Minting here is
            # authorised: this tool runs as a bearer-authenticated user, and
            # the rows are already filtered to that caller below.
            "reauth_url": google_login_url(t.account_email, u.name),
        }
        for t, u in (
            session.query(OAuthToken, User)
            .join(User, OAuthToken.user_id == User.id)
            .filter(OAuthToken.needs_reauth_at.isnot(None))
            .all()
        )
        # OAuthToken is UserOwnedMixin — a re-auth link for the other
        # user's account is not this caller's problem to act on.
        if scope_user_id is None or t.user_id == scope_user_id
    ]

    # Axis 4: tool-call health, from the `runs` ledger's `kind="tool_call"`
    # rows (per-call dispatch log in app/mcp/server.py + app/api/v1.py,
    # persisted via app.services.runs.record_tool_call — Wave 5.1 absorbed
    # the standalone `tool_calls` table into `runs`). Independent of the
    # sync-based axes above — a tool can be broken (bad args handling, a
    # downstream API returning garbage) while its integration's own sync job
    # is perfectly healthy. Household-wide, unscoped by caller — same as
    # before the merge — this is an ops diagnostic over every caller's tool
    # calls, not a per-user view.
    tool_alerts: list[dict] = []

    failure_cutoff = datetime.now(timezone.utc) - timedelta(minutes=TOOL_FAILURE_LOOKBACK_MINUTES)
    failing_tools = (
        session.query(Run.name, func.count(Run.id).label("failures"))
        .filter(Run.kind == "tool_call", Run.outcome == "error", Run.started_at >= failure_cutoff)
        .group_by(Run.name)
        .having(func.count(Run.id) > TOOL_FAILURE_THRESHOLD)
        .all()
    )
    for row in failing_tools:
        tool_alerts.append({
            "tool": row.name,
            "issue": (
                f"failing repeatedly ({row.failures}x errors in the last "
                f"{TOOL_FAILURE_LOOKBACK_MINUTES}m)"
            ),
        })

    p95_cutoff = datetime.now(timezone.utc) - timedelta(hours=TOOL_P95_LOOKBACK_HOURS)
    p95_expr = func.percentile_cont(0.95).within_group(Run.duration_ms.asc())
    slow_tools = (
        session.query(
            Run.name,
            p95_expr.label("p95_duration_ms"),
            func.count(Run.id).label("calls"),
        )
        .filter(Run.kind == "tool_call", Run.started_at >= p95_cutoff)
        .group_by(Run.name)
        .having(func.count(Run.id) >= TOOL_P95_MIN_CALLS)
        .all()
    )
    for row in slow_tools:
        if row.p95_duration_ms is not None and row.p95_duration_ms > TOOL_P95_THRESHOLD_MS:
            tool_alerts.append({
                "tool": row.name,
                "issue": (
                    f"p95 duration {int(row.p95_duration_ms)}ms over last "
                    f"{TOOL_P95_LOOKBACK_HOURS}h ({row.calls} calls)"
                ),
            })

    # Axis 5: daemon liveness + per-task health (F11b), from `client_tokens`
    # heartbeat data (`app/api/v1.py::heartbeat`). A non-empty `client_version`
    # is the discriminator between a comar-client daemon token and any other
    # active token (e.g. a phone's Health Auto Export bearer, which never
    # reports a version) — only daemons report either field.
    #
    # Reuses the same `_alert()` sink as axis 1, keyed on the token's label
    # instead of an integration name. That's deliberate: it means a struggling
    # daemon flows through the exact same `alerts` list, and therefore the
    # exact same `integration:{name}:{kind}` fingerprint path the notifications
    # sweep already uses for axis 1 — no changes needed there for this to
    # reach ntfy. Fingerprints must never key on rendered text (which carries
    # an age that changes every sweep); each issue string here is written so
    # its *kind* (the words before the first parenthesis) is stable regardless
    # of the numbers inside.
    #
    # Scoping: a per-user caller sees only their own daemons — client_tokens
    # is per-user data, and the leak-canary sweep in tests/test_user_scoping.py
    # enforces that. The household view (scope_user_id=None — the notifications
    # sweep and the dashboard route) sees the whole fleet: daemon liveness is
    # household infrastructure monitoring pushed to the single household ntfy
    # topic, the same admin-household exception the dashboard summaries
    # already use.
    silent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=_daemon_silent_minutes())
    daemon_query = (
        session.query(ClientToken)
        .filter(ClientToken.is_active.is_(True))
        .filter(ClientToken.client_version.isnot(None))
        .filter(ClientToken.client_version != "")
    )
    caller_uid = scope_user_id
    if caller_uid is not None:
        daemon_query = daemon_query.filter(ClientToken.user_id == caller_uid)
    daemon_tokens = daemon_query.all()

    # Structured per-daemon status (R2, 2026-09) — parallel to `data_freshness`
    # above but for the client daemon rather than an integration's sync.
    # Exists *in addition to* the issue-text alerting the loop below already
    # does: those strings answer "is anything wrong"; this list answers "what
    # is this daemon's actual state right now" (heartbeat age, version,
    # whether the SSE write channel it needs for reminders is attached) —
    # visible even when nothing is currently alerting, same reason
    # `data_freshness` is populated whether or not each row is stale.
    # `sse_connected` reuses axis 6's own signal (`stream_manager.
    # connected_users()`) rather than re-deriving it, and per-user for the
    # same reason axis 5's alerting already is: one live daemon must never
    # mask another's dead one.
    try:
        from app.stream_manager import stream_manager as _stream_manager

        connected_names = {u.lower() for u in _stream_manager.connected_users()}
    except Exception:  # noqa: BLE001
        logger.exception("restore drill/daemon axis: stream_manager unavailable")
        connected_names = set()

    from app.models.users import User as _User

    user_names = {u.id: u.name for u in session.query(_User).all()}

    daemon_status: list[dict] = []
    now_utc = datetime.now(timezone.utc)
    for token in daemon_tokens:
        label = token.label or f"token-{token.id}"
        heartbeat_age = (
            int((now_utc - token.last_seen_at).total_seconds())
            if token.last_seen_at is not None
            else None
        )
        owner_name = user_names.get(token.user_id)
        daemon_status.append({
            "label": label,
            "user_id": token.user_id,
            "scope": token.scope,
            "client_version": token.client_version,
            "last_seen_at": token.last_seen_at.isoformat() if token.last_seen_at else None,
            "last_heartbeat_age": heartbeat_age,
            "sse_connected": bool(owner_name) and owner_name.lower() in connected_names,
        })

        if token.last_seen_at is None:
            issue = "daemon silent (never seen)"
            _alert(label)["issues"].append(issue)
            _attribute(label, issue, token.user_id)
        elif token.last_seen_at < silent_cutoff:
            age = int((datetime.now(timezone.utc) - token.last_seen_at).total_seconds())
            issue = f"daemon silent (last seen {format_age(age)} ago)"
            _alert(label)["issues"].append(issue)
            _attribute(label, issue, token.user_id)

        if not token.task_health:
            continue
        try:
            tasks = json.loads(token.task_health)
        except (TypeError, ValueError):
            continue
        if not isinstance(tasks, dict):
            continue
        for task_name, info in tasks.items():
            if not isinstance(info, dict):
                continue
            restarts = info.get("restarts") or 0
            last_error = info.get("last_error")
            finished = bool(info.get("finished"))
            details = []
            if restarts > DAEMON_TASK_RESTART_THRESHOLD:
                details.append(f"restarts={restarts}")
            if last_error:
                details.append(f"last_error={str(last_error)[:100]}")
            if finished:
                details.append("finished")
            if details:
                issue = f"task {task_name} unhealthy ({', '.join(details)})"
                _alert(label)["issues"].append(issue)
                _attribute(label, issue, token.user_id)

    # Axis 6: the reminders *write* channel (2026-08-19). Every axis above
    # measures data *arriving*; this one measures commands *leaving*. They are
    # independent, and conflating them hid a real outage: `apple_reminders` read
    # fresh at 0m all evening while three `reminders_complete` calls were being
    # dropped, because inbound pushes from the phone were genuinely fine.
    #
    # A liveness signal that measures one direction is not a liveness signal.
    #
    # The *join* is the point. The daemon cannot detect this alone — during the
    # outage its own /health reported status ok, vault watcher active, retry
    # queue 0, reminders task alive 23s ago. Every signal it owns was green,
    # because a dead SSE subscription looks identical from the subscriber's
    # side. Only the server knows whether anyone is subscribed; only the DB
    # knows whether writes are waiting. Nothing put those two facts together.
    try:
        from app.stream_manager import stream_manager

        reminders_facade = get_capability("reminders.query")
        connected = {u.lower() for u in stream_manager.connected_users()}
        max_age = reminders_facade.max_replay_age_seconds
        for entry in reminders_facade.pending_writes(session):
            owner_id = entry["user_id"]
            if scope_user_id is not None and owner_id != scope_user_id:
                continue
            if entry["user_name"].lower() in connected:
                # Subscribed: the queue is draining, or about to. A couple of
                # in-flight commands is the normal steady state, not an alert.
                continue
            issue = (
                f"reminder writes not reaching {_user_label(session, owner_id)}'s Mac "
                f"({entry['count']} queued, oldest {format_age(entry['oldest_age_seconds'] or 0)}, "
                f"no daemon subscribed; expire after {format_age(max_age)})"
            )
            _alert("apple_reminders")["issues"].append(issue)
            _attribute("apple_reminders", issue, owner_id)
    except Exception:  # noqa: BLE001
        logger.exception("reminders write-channel check failed")

    # Axis 7: the host's disk (2026-09-02). The one failure that takes
    # everything down at once and looks like something else while it does:
    # at 100 % Postgres PANICs mid-checkpoint and crash-loops in recovery, and
    # the app reports a connection error that reads as networking. It has now
    # happened twice (31 GB in July, 61 GB today), both times from image pulls.
    # The container's `/` is an overlay on the host filesystem, so the figure
    # is the host's. Household-wide: a full disk is nobody's data.
    disk_issue = _disk_issue()
    if disk_issue:
        _alert("host")["issues"].append(disk_issue)

    # Axis 8: the restore drill (R2, 2026-09-05). A backup nobody has ever
    # restored is a hope, not a backup — dumps stopped silently once already
    # (2026-07-14). Household-wide: infra fact, not owned by whoever happens
    # to be asking. See app/integrations/system/restore_drill.py.
    try:
        from app.integrations.system import restore_drill

        drill_status = restore_drill.evaluate_alert(session)
    except Exception:  # noqa: BLE001
        logger.exception("restore drill alert axis failed")
        drill_status = {
            "days_since_restore_drill": None,
            "last_outcome": "check_failed",
            "issues": [],
        }
    for issue in drill_status["issues"]:
        _alert("restore_drill")["issues"].append(issue)

    # Axis 9: index state (R4, 2026-09-04). "Is search still catching up
    # right now" used to mean a psql session against `embedding_queue` —
    # this makes it a field. Household-wide and cross-source (vault, email,
    # whatsapp, corpus, coffee, tasks all share one queue/worker), because a
    # caller asking whether search results can be trusted yet doesn't care
    # which producer is behind. Always present, same reasoning as
    # `restore_drill`/`daemon_status` above: "fully indexed" and "never
    # measured" must never look the same, and this can't look the same as
    # either because it's a live count, not a probe that can go unmeasured.
    index_state = _index_state(session)

    # Axis 11: device fleet — Strand A dead-board alerting
    # (`vault/Projects/lios/Plans/house-management-2026-08.md`). Household-wide
    # regardless of scope_user_id, same reasoning as axis 7/8 above: HA is
    # household-shared infrastructure, not owned by whoever happens to ask
    # (the plan says so explicitly — "not per-user, unlike F11").
    #
    # ⚠️ An empty/missing/unparseable registry is a HARD REFUSAL — see
    # `device_fleet.FleetRegistryError`'s docstring — so this is deliberately
    # NOT folded into the same silent-`logger.exception`-and-continue shape
    # every other axis above uses. That shape is right for "one axis's data
    # source is briefly unavailable"; it is wrong for "the compliance
    # registry itself could not be read", which must read as loud as any
    # `dead` board, never as "no boards, nothing wrong".
    try:
        fleet_status = device_fleet.evaluate_alert(session)
    except device_fleet.FleetRegistryError as exc:
        logger.error("device fleet registry refused: %s", exc)
        device_fleet_payload = {"boards": [], "registry_error": str(exc)}
        _alert("device_fleet")["issues"].append(
            f"device fleet registry unavailable: {exc}"
        )
    except Exception:  # noqa: BLE001
        logger.exception("device fleet alert axis failed unexpectedly")
        device_fleet_payload = {"boards": [], "registry_error": "check_failed"}
        _alert("device_fleet")["issues"].append(
            "device fleet check failed unexpectedly"
        )
    else:
        device_fleet_payload = {"boards": fleet_status["boards"], "registry_error": None}
        for issue in fleet_status["issues"]:
            _alert("device_fleet")["issues"].append(issue)

    # Axis 12: host fleet — liveness alerting one layer below device_fleet
    # (hosts, not boards). Household-wide regardless of scope_user_id, same
    # reasoning as device_fleet above: infrastructure is not owned by
    # whoever happens to ask.
    #
    # ⚠️ Same hard-refusal shape as device_fleet — an empty/missing/
    # unparseable contracts/hosts.md must read as loud as any `dead` host,
    # never as "no hosts, nothing wrong". See host_fleet.HostRegistryError's
    # docstring.
    try:
        host_status = host_fleet.evaluate_alert(session)
    except host_fleet.HostRegistryError as exc:
        logger.error("host registry refused: %s", exc)
        host_fleet_payload = {"hosts": [], "registry_error": str(exc)}
        _alert("host_fleet")["issues"].append(
            f"host registry unavailable: {exc}"
        )
    except Exception:  # noqa: BLE001
        logger.exception("host fleet alert axis failed unexpectedly")
        host_fleet_payload = {"hosts": [], "registry_error": "check_failed"}
        _alert("host_fleet")["issues"].append(
            "host fleet check failed unexpectedly"
        )
    else:
        host_fleet_payload = {"hosts": host_status["hosts"], "registry_error": None}
        for issue in host_status["issues"]:
            _alert("host_fleet")["issues"].append(issue)

    # Axis 10: recent_runs (S5.1, `vault/Projects/lios/Backlog.md`; bounded
    # and summarised Wave 5.11). "What ran in the last hour" answered from
    # the `runs` ledger (Wave 5.1: scheduled jobs AND tool calls, one table)
    # instead of the logs. Separate window from `threshold_minutes` above
    # (that one gates *staleness*; this one is just "how far back to list
    # activity") — `recent_runs_minutes`, default 60. Scoping matches axis 5:
    # scheduled_job/manual rows (almost entirely household-wide) are never
    # filtered by caller; tool_call rows (real per-call user_id) are scoped
    # to the caller when one is bound.
    #
    # Measured on the live system 2026-09-05: the 60-minute window held 147
    # rows and `whatsapp_bridge_heartbeat` (a job every minute) was most of
    # them — 1,440 identical `ok` rows a day from one job. This axis now
    # returns a by-name summary (`app.services.runs.recent_activity_summary`,
    # sharing its query/scoping with `recent_activity` so `system_runs` and
    # this axis can't drift on what "recent activity" means) plus a capped
    # list of only the non-ok runs (`recent_runs_items_limit`, default 20) —
    # `by_name`'s `count`/`worst_outcome` is what still makes a stopped
    # heartbeat visible even though it's no longer ledgered as a
    # per-execution `runs` row (see the whatsapp manifest's `ledger=False`
    # and its comment) via axis 1's own SyncState-based staleness check.
    from app.services.runs import recent_activity_summary

    recent_runs_minutes = int(arguments.get("recent_runs_minutes", 60))
    recent_runs_items_limit = int(arguments.get("recent_runs_items_limit", 20))
    recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=recent_runs_minutes)
    recent_runs = recent_activity_summary(
        session,
        since=recent_cutoff,
        scope_user_id=scope_user_id,
        items_limit=recent_runs_items_limit,
    )

    # Final pass, scoped view only: drop entries for per-user integrations
    # this caller has no data in at all. This is what makes "commute" and
    # "google_mail" alerts vanish from Sam's view when she's never touched
    # either — nothing to hand-list, `_per_user_integration_names()` derives
    # it from `UserOwnedMixin`. Axis 5 (daemon) entries are keyed by a token
    # label, not an integration name, so they never match here and are left
    # exactly as axis 5 already scoped them above.
    if scope_user_id is not None:
        per_user_names = _per_user_integration_names()
        alerts_by_integration = {
            name: entry
            for name, entry in alerts_by_integration.items()
            if name not in per_user_names
            or _user_has_data_for_integration(session, name, scope_user_id)
        }

    alerts = list(alerts_by_integration.values())
    return {
        "status": "all_ok" if not alerts and not reauth_needed and not tool_alerts else "degraded",
        "alerts": alerts,
        "data_freshness": freshness,
        # Names every integration with NO staleness probe at all — distinct
        # from the `data_freshness` rows above, which are integrations that
        # *are* probed (and currently read as healthy, or their issue would
        # already be in `alerts`). Household-wide regardless of `scope_user_id`:
        # "nobody is watching this integration" is a platform fact, not
        # something owned by whichever caller happened to ask. `commute` is
        # never in this list even during its out-of-window hours (see
        # `unmeasured_integrations`'s docstring) — it has a probe, it's just
        # not evaluated right now, which is a different state from never
        # having been instrumented.
        "unmeasured": unmeasured_integrations(),
        "reauth_needed": reauth_needed,
        "tool_alerts": tool_alerts,
        # R2, 2026-09-05: always present (not just when degraded), same
        # reasoning as `unmeasured` above — "healthy" and "never checked"
        # must never look the same. `last_outcome` is `never_run` if the
        # drill has never executed on this box.
        "restore_drill": {
            "days_since_restore_drill": drill_status["days_since_restore_drill"],
            "last_outcome": drill_status["last_outcome"],
        },
        # R2, 2026-09-05: per-daemon heartbeat/version/SSE-attachment, always
        # present regardless of alerting state — see the comment where this
        # is built, above axis 5's loop. Household-wide list, scoped to the
        # caller's own daemon(s) exactly like axis 5 already is (the query
        # `daemon_tokens` was built from is filtered on `caller_uid` above).
        "daemon_status": daemon_status,
        # R4, 2026-09-04: embedding queue rebuild state — see the comment
        # above where `index_state` is built.
        "index_state": index_state,
        # S5.1, 2026-09-05, bounded + summarised Wave 5.11: "what ran in the
        # last `recent_runs_minutes` minutes" from the runs ledger (scheduled
        # jobs + tool calls, one table since Wave 5.1), not the logs. Always
        # present (empty `by_name`/items/counts when nothing ran), same
        # reasoning as `daemon_status`/`index_state` above — `by_name` is a
        # summary of everything in the window, `items` is only the non-ok
        # runs (capped, see `recent_activity_summary`).
        "recent_runs": recent_runs,
        # Strand A, 2026-09-08: per-board liveness — always present, same
        # "never look the same as never-checked" reasoning as `restore_drill`/
        # `daemon_status`/`index_state` above. `registry_error` is non-None
        # exactly when the fleet registry itself could not be read (see the
        # comment above where `device_fleet_payload` is built) — a hard
        # refusal, never folded into a clean `boards: []`.
        "device_fleet": device_fleet_payload,
        # Axis 12, 2026-09-08: per-host liveness — same "never look the
        # same as never-checked" reasoning as device_fleet above.
        # `registry_error` is non-None exactly when contracts/hosts.md
        # itself could not be read — a hard refusal, never folded into a
        # clean `hosts: []`.
        "host_fleet": host_fleet_payload,
    }


def handle_runs(session: Session, arguments: dict[str, Any]) -> str:
    """Filterable, read-only view over the runs ledger (S5.1; scheduled
    jobs and tool calls in one table since Wave 5.1).

    `system_alerts`' `recent_runs` axis answers "what ran in the last hour"
    with a fixed, small window; this is the same underlying view
    (`app.services.runs.recent_activity`) with a longer/explicit `since`
    and filters, for "did X actually run today" / "what's been failing"
    questions that don't fit an alerts payload.

    Scoping matches `recent_runs`: scheduled_job/manual rows (almost
    entirely household-wide) are never filtered by caller; tool_call rows
    are scoped to the caller's own rows when one is bound.
    """
    from app.auth.context import current_user_id_or_none
    from app.services.runs import recent_activity

    since_arg = arguments.get("since")
    if since_arg:
        try:
            since = datetime.fromisoformat(since_arg.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError:
            return json.dumps({"error": f"invalid `since`: {since_arg!r} (expected ISO 8601)"})
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=24)

    result = recent_activity(
        session,
        since=since,
        scope_user_id=current_user_id_or_none(),
        name=arguments.get("name") or None,
        outcome=arguments.get("outcome") or None,
        # Wave 5.11: default lowered 200 -> 100. `recent_activity` already
        # honours `limit` via its own `.limit(limit)` query — the default
        # was simply larger than most callers need for "a filterable view",
        # now that `recent_runs` (the axis this feeds alongside) carries its
        # own bounded summary shape instead of a raw row dump.
        limit=min(int(arguments.get("limit", 100)), 500),
    )
    return json.dumps(result, indent=2)


def handle_ai_usage(session: Session, arguments: dict[str, Any]) -> str:
    """What has AI cost, and on what.

    Reads the `ai_usage` ledger — one row per AI call, rates stored at call
    time — and answers the two questions it was built for: what did *that*
    call cost (the most recent rows, in full), and where is the money going
    (totals by role and model over a window). Added 2026-09-02 when the first
    54-minute transcription raised exactly that question and the only way to
    answer it was a psql session.

    `cost_usd` is NULL for models `coglib.llm.MODELS` does not price — STT and
    embedding rows among them — and NULL means unknown, never free. Totals
    therefore report `unpriced_calls` alongside `cost_usd`, so a small number
    is never mistaken for a complete one.
    """
    from app.models.ai_usage import AiUsage

    days = max(1, min(int(arguments.get("days", 30)), 365))
    limit = max(1, min(int(arguments.get("limit", 10)), 100))
    since = datetime.now(timezone.utc) - timedelta(days=days)

    q = session.query(AiUsage).filter(AiUsage.ts >= since)
    for key in ("role", "kind", "caller", "model"):
        if arguments.get(key):
            q = q.filter(getattr(AiUsage, key) == arguments[key])

    recent = q.order_by(AiUsage.ts.desc()).limit(limit).all()

    def _row(r: AiUsage) -> dict[str, Any]:
        return {
            "ts": r.ts.isoformat() if r.ts else None,
            "role": r.role, "kind": r.kind, "caller": r.caller,
            "provider": r.provider, "model": r.model,
            "tokens": {"in": r.units_in, "out": r.units_out, "reasoning": r.reasoning_units},
            "seconds": r.seconds, "latency_ms": r.latency_ms,
            "cost_usd": None if r.cost_usd is None else round(r.cost_usd, 4),
            "rates_per_1m": {"in": r.input_rate, "out": r.output_rate},
            "ok": r.ok, "error": r.error,
        }

    def _totals(group_col):
        rows = (
            q.with_entities(
                group_col,
                func.count(AiUsage.id),
                func.sum(AiUsage.cost_usd),
                func.sum(AiUsage.units_in), func.sum(AiUsage.units_out), func.sum(AiUsage.reasoning_units),
                func.sum(func.cast(AiUsage.cost_usd.is_(None), Integer)),
                func.sum(func.cast(~AiUsage.ok, Integer)),
            )
            .group_by(group_col)
            .order_by(func.sum(AiUsage.cost_usd).desc().nullslast())
            .all()
        )
        return [
            {
                "key": k or "(none)", "calls": n,
                "cost_usd": None if c is None else round(float(c), 4),
                "tokens": {"in": int(ti or 0), "out": int(to or 0), "reasoning": int(tr or 0)},
                "unpriced_calls": int(unp or 0), "failed_calls": int(bad or 0),
            }
            for k, n, c, ti, to, tr, unp, bad in rows
        ]

    total_cost = q.with_entities(func.sum(AiUsage.cost_usd)).scalar()
    total_calls = q.with_entities(func.count(AiUsage.id)).scalar() or 0
    unpriced = q.filter(AiUsage.cost_usd.is_(None)).count()

    return json.dumps({
        "window_days": days,
        "filters": {k: arguments[k] for k in ("role", "kind", "caller", "model") if arguments.get(k)},
        "total": {
            "calls": total_calls,
            "cost_usd": None if total_cost is None else round(float(total_cost), 4),
            "unpriced_calls": unpriced,
        },
        "by_role": _totals(AiUsage.role),
        "by_model": _totals(AiUsage.model),
        "recent": [_row(r) for r in recent],
        "note": (
            "cost_usd is NULL when the model has no rate in coglib.llm.MODELS — "
            "unknown, never free. Rates are the ones in force at call time; "
            "reasoning tokens are billed at the output rate."
        ),
    }, indent=2)


def handle_what_lios_sees(session: Session, arguments: dict[str, Any]) -> str:
    """"What does lios see about me?" — per-caller, one call, no psql.

    Three sections, built from the SAME classification `tests/
    test_user_scoping.py` enforces (`app.privacy`), so a new table shows up
    here automatically the day it's added rather than needing this tool
    hand-edited to know about it:

    1. `private` — every `UserOwnedMixin` model (per-user data): which
       integration owns it, a human label, THIS caller's row count only
       (never another user's — every count below is a `WHERE user_id = :uid`
       query), earliest/latest activity where the table has a timestamp
       column, and whether the integration is "connected" for this caller
       specifically (`_user_has_data_for_integration`, the same per-user
       presence check `system_alerts` already uses — reused here rather
       than reimplemented, and per-user rather than the integration-wide
       `is_configured()`, which would tell Sam Gmail is "configured"
       because ALEX connected his).
    2. `shared_by_design` — `app.privacy.HOUSEHOLD_SHARED_TABLES`, labelled
       with why each one is intentionally visible to both users. This is
       the same dict `tests/test_user_scoping.py` asserts against the
       schema, so what this tool tells Sam is shared is exactly what the
       test also treats as shared — one source of truth, not two lists
       that could quietly disagree.
    3. `not_stored` — a short, code-verified (not asserted-from-memory) list
       of things lios deliberately does NOT keep, so "does it see X" has an
       explicit no rather than silence.
    """
    from sqlalchemy import or_

    from app.auth.context import current_user_id
    from app.privacy import (
        HOUSEHOLD_SHARED_TABLES,
        integration_for_model,
        integration_for_table,
        label_for,
        timestamp_column_for,
        user_columns_for,
        user_owned_models,
    )

    uid = current_user_id()

    private_rows: list[dict[str, Any]] = []
    connected_cache: dict[str, bool] = {}

    for model in user_owned_models():
        table = model.__tablename__
        integration = integration_for_model(model)
        label = label_for(table, integration)

        # `user_columns_for` returns MULTIPLE columns for a table like
        # `vault_read_grants` (grantee_user_id / owner_user_id) — this
        # caller's rows are the ones where they match ANY of them, same
        # rule `scoped_query` enforces on the read path (see app/tools/
        # helpers.py). A single-column UserOwnedMixin table is unaffected —
        # `user_cols` is just `[model.user_id]`.
        user_cols = [getattr(model, name) for name in user_columns_for(model)]
        owner_filter = or_(*(col == uid for col in user_cols))

        count = (
            session.query(func.count()).select_from(model)
            .filter(owner_filter)
            .scalar()
        ) or 0

        earliest = latest = None
        ts_col_name = timestamp_column_for(model)
        if ts_col_name and count:
            ts_col = getattr(model, ts_col_name)
            earliest, latest = (
                session.query(func.min(ts_col), func.max(ts_col))
                .filter(owner_filter)
                .one()
            )

        if integration not in connected_cache:
            if integration == "system":
                # Account/app infrastructure (tokens, preferences, device
                # logs) — "connected" doesn't mean anything separate from
                # "has rows" here, there's no external service to be
                # configured against.
                connected_cache[integration] = None
            else:
                connected_cache[integration] = _user_has_data_for_integration(
                    session, integration, uid
                )

        private_rows.append({
            "table": table,
            "integration": integration,
            "label": label,
            "row_count": int(count),
            "earliest": earliest.isoformat() if earliest else None,
            "latest": latest.isoformat() if latest else None,
            "connected": connected_cache[integration],
        })

    private_rows.sort(key=lambda r: (r["integration"], r["table"]))

    shared_rows = [
        {
            "table": table,
            "integration": integration_for_table(table),
            "reason": reason,
        }
        for table, reason in sorted(HOUSEHOLD_SHARED_TABLES.items())
    ]

    # Verified from code, not asserted from memory (see this handler's
    # docstring): app/integrations/whatsapp/models.py's WhatsAppMessage has
    # `body` (text) and `media_caption` (text) but no binary/blob column —
    # media itself never lands in Postgres. app/integrations/attachments/
    # models.py's MessageAttachment is metadata-only (filename/mime/size)
    # until `attachments_ingest` is explicitly called, at which point (and
    # only then) the file downloads to `storage_path` on disk.
    not_stored = [
        "WhatsApp photos/audio/video themselves — only the message text "
        "and, for media messages, the caption are stored. The media bytes "
        "are never downloaded or kept.",
        "Email/WhatsApp attachments are metadata only (filename, type, "
        "size) until you explicitly run attachments_ingest on one — the "
        "file itself isn't downloaded before then.",
    ]

    total_private_rows = sum(r["row_count"] for r in private_rows)
    tables_with_data = [r for r in private_rows if r["row_count"] > 0]
    if tables_with_data:
        by_label = ", ".join(
            f"{r['label']} ({r['row_count']})" for r in tables_with_data
        )
        summary = (
            f"lios holds {total_private_rows} rows of your own private data "
            f"across {len(tables_with_data)} table(s): {by_label}. It also "
            f"shares the household's calendar, finance, weather, renovation "
            f"snags, historical documents, Home Assistant status and task "
            f"ledger with both of you by design — never your private data "
            f"with the other person, or theirs with you."
        )
    else:
        summary = (
            "lios doesn't hold any private data for you yet. It shares the "
            "household's calendar, finance, weather, renovation snags, "
            "historical documents, Home Assistant status and task ledger "
            "with both of you by design — nothing private is ever shared "
            "between the two of you."
        )

    return json.dumps({
        "summary": summary,
        "private": private_rows,
        "shared_by_design": shared_rows,
        "not_stored": not_stored,
    }, indent=2)


def handle_alert_log(session: Session, arguments: dict[str, Any]) -> str:
    """Reviewable log of monitoring alerts (lios#230) — what fired/cleared
    since `since`, deduped by fingerprint, split into `fired`/`cleared`
    with counts by `page == "phone"` vs FYI. Wraps the `alerts.query`
    capability (`app.integrations.alerts.facade.FACADE.events_since`) —
    this tool exists here rather than on `alerts` itself because the daily
    brief already composes its "Monitoring since last note" sub-block from
    the same call (see `brief.py`'s `alert_log` source and
    `brief_render.render_alerts`), and `system` is where every other
    cross-integration composite tool lives.
    """
    from app.tools.base import parse_iso_date

    since_arg = arguments.get("since")
    since = parse_iso_date(since_arg) if since_arg else None
    if since is None:
        raise ValueError(f"'since' is required and must be a valid ISO timestamp (got {since_arg!r})")

    limit = min(int(arguments.get("limit", 200)), 500)
    alerts_facade = get_capability("alerts.query")
    return alerts_facade.events_since(session, {"since": since_arg, "limit": limit})


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions for system diagnostics and composite routines."""
    return [
        CustomTool(
            name="system_alerts",
            description=(
                "Check for integration health issues. Returns any integrations "
                "that are failing (consecutive errors) or stale (not synced within "
                "the threshold). Use this at the start of daily notes to surface "
                "warnings about data freshness. Also returns `unmeasured`: "
                "integrations with no staleness probe at all, so a status of "
                "'all_ok' isn't mistaken for full coverage — an integration in "
                "that list has never been checked, not confirmed healthy. Also "
                "returns `restore_drill` (days since the last successful "
                "restore-drill run and its outcome — 'never_run' is distinct "
                "from healthy), `daemon_status` (per-daemon last "
                "heartbeat age, client version, and whether its SSE write "
                "channel is attached), and `recent_runs` (a by-name summary "
                "of what ran in the last `recent_runs_minutes`, from the "
                "runs ledger — scheduled jobs and tool calls both — with "
                "non-ok names sorted first and only the non-ok runs listed "
                "in full under `items`, capped at `recent_runs_items_limit`; "
                "see `system_runs` for the full filterable row view)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threshold_minutes": {
                        "type": "integer",
                        "description": "Minutes of staleness before alerting (default: 60).",
                        "default": 60,
                    },
                    "recent_runs_minutes": {
                        "type": "integer",
                        "description": "Window for `recent_runs`, in minutes (default: 60).",
                        "default": 60,
                    },
                    "recent_runs_items_limit": {
                        "type": "integer",
                        "description": (
                            "Max non-ok runs listed under `recent_runs.items` "
                            "(default: 20). Doesn't affect `by_name`, which "
                            "always summarises every name seen in the window."
                        ),
                        "default": 20,
                    },
                },
            },
            handler=handle_alerts,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "Are any integrations broken?",
                "Check system health",
            ],
        ).build(),
        CustomTool(
            name="system_morning_briefing",
            description=(
                "Get a complete morning overview in one call. Returns today's calendar events, "
                "current weather and today's forecast, all incomplete reminders, unread emails, "
                "health summary (steps, sleep, heart rate), home status (Home Assistant), commute "
                "status (weekday mornings only), and system alerts. Use this to build a daily note "
                "or get a quick start-of-day snapshot."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=handle_morning_briefing,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "What's my morning look like?",
                "Give me a morning briefing",
                "What do I need to know today?",
            ],
        ).build(),
        CustomTool(
            name="system_daily_brief",
            description=(
                "Everything the daily note needs, in ONE call: system alerts, today's "
                "calendar, weather now + forecast, open reminders and what changed since "
                "a given time, transport departures, home status and appliance history, "
                "unread + recent email, recent WhatsApp, pending attachments, pending "
                "inbox files, health summary/sleep/trends/workouts, recent listening and "
                "weekly stats, and current coffee + recent brews.\n\n"
                "Prefer this over calling those tools individually — it is one round-trip "
                "instead of ~23, runs its sources in parallel, and serves the slow ones "
                "from a short-lived cache while always fetching the real-time ones live.\n\n"
                "Sections the caller has no data for are skipped automatically, so the "
                "payload reflects what this user actually uses. NOTE: this is read-only "
                "and deliberately excludes snag_capture, which writes — call that "
                "separately if you want the Snags section.\n\n"
                "Pass render=true to also get a top-level `rendered: {section: markdown}` "
                "block with finished, ready-to-paste fragments for the purely mechanical "
                "sections (alerts, pulse, coffee, transport, consumables, snags, calendar, "
                "listening, freshness) — every number in them is read straight off this "
                "same payload, never off a previous note. Sections needing real judgement "
                "(email/WhatsApp triage, the morning assessment, coaching opinions) are not "
                "rendered and still need composing by hand.\n\n"
                "The unfiltered payload is LARGE (routinely 100-150k chars) and will "
                "exceed most clients' inline tool-output ceiling, so expect the result "
                "to be spooled to a file rather than returned inline — that is normal, "
                "not an error. Do not read the spool file top to bottom: every source "
                "sits under its own top-level key, so pull only the keys you need "
                "(e.g. `jq '._meta' <file>`, `jq '.calendar' <file>`)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": (
                            "ISO timestamp of the previous daily note. Used to report what "
                            "changed in reminders since then, AND — since 2026-09-07 — "
                            "narrows the mail/WhatsApp comms window when it's later than "
                            "the default lookback (e.g. a note written this morning makes "
                            "'since then' tighter than 'since Friday'); never widens past "
                            "the default. `_meta.window_source` says which one won "
                            "('since' or 'lookback'). Omit for a 24h default."
                        ),
                    },
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Fetch only the sources feeding these sections (e.g. "
                            "[\"email\", \"transport\"]). Omit for everything. System "
                            "alerts are always included. Accepts either the preference "
                            "vocabulary (pulse, food, coffee, transport, house, snags, "
                            "today, tasks, email, whatsapp, meetings, notes) or the "
                            "rendered vocabulary used by `rendered`/the prompt templates "
                            "(alerts, pulse, coffee, transport, consumables, snags, "
                            "calendar, listening, freshness) - calendar means today, "
                            "consumables means house, and listening means pulse. An "
                            "unrecognised name is reported in `_meta.sections_unknown` "
                            "rather than silently matching nothing."
                        ),
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": "Bypass the cache and re-fetch every source.",
                        "default": False,
                    },
                    "render": {
                        "type": "boolean",
                        "description": (
                            "Also return `rendered: {section: markdown}` — finished, "
                            "ready-to-paste fragments for the mechanical sections (alerts, "
                            "pulse, coffee, transport, consumables, snags, calendar, "
                            "listening, freshness), computed deterministically from this "
                            "same payload. Default false keeps the existing shape unchanged."
                        ),
                        "default": False,
                    },
                },
            },
            handler=handle_daily_brief,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "Build my daily note",
                "What do I need to know this morning?",
                "Refresh the transport and email sections",
            ],
        ).build(),
        CustomTool(
            name="conversations_since",
            description=(
                "Group WhatsApp and Gmail messages received since a timestamp into "
                "conversations, DETERMINISTICALLY — no embedding call, no LLM call. "
                "A 'conversation' is: for Gmail, one group per thread_id (Gmail's own "
                "threading); for WhatsApp, one group per chat per burst, where a burst "
                "is messages in that chat separated by no more than "
                "system.conversations_burst_gap_minutes (default 6 hours). Groups are "
                "sorted by last-message time, newest first.\n\n"
                "Use this instead of scoring/tiering individual messages — a message "
                "read alone can misread urgency (a question already answered later in "
                "the same thread, a passing remark read as an escalation). Each group "
                "carries participants, first/last message, a capped message list "
                "(oldest first, up to 20) so the full exchange can be read in context, "
                "a deterministic has_question flag, and message_count/from_me_count so "
                "a caller can tell FYI from something that needs a decision.\n\n"
                "Excluded: groups where every message is from the caller (nothing to "
                "review), and WhatsApp's own self-notes channel (not a conversation)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "ISO timestamp. Required. Refused if more than 30 days ago.",
                    },
                    "until": {
                        "type": "string",
                        "description": "ISO timestamp. Optional, default now.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max conversation groups returned, newest-first (default 30, max 100).",
                        "default": 30,
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["whatsapp", "mail"]},
                        "description": "Subset of whatsapp/mail. Default: both.",
                    },
                },
                "required": ["since"],
            },
            handler=handle_conversations_since,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "What conversations happened on WhatsApp and email since this morning?",
                "Group my messages from the last day into conversations",
            ],
        ).build(),
        CustomTool(
            name="system_week_ahead",
            description=(
                "Get a week-ahead overview: 7 days of calendar events, all incomplete "
                "reminders, and the full weather forecast. Use this for weekly planning "
                "or to see what's coming up."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=handle_week_ahead,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "What's coming up this week?",
                "Plan my week",
                "What's on for the next few days?",
            ],
        ).build(),
        CustomTool(
            name="system_search_everything",
            description=(
                "Search across vault notes, emails, and WhatsApp messages simultaneously "
                "using semantic (meaning-based) search. Returns the top matches from each "
                "source. Use this when you're looking for something but don't know where it is."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for — natural language works best.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results per source (default 5, max 20).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            handler=handle_search_everything,
            annotations=_READ_ONLY,
            category="search",
            examples=[
                "Find anything about the renovation budget",
                "Search for the school schedule",
                "What did we say about the plumber?",
            ],
        ).build(),
        CustomTool(
            name="system_ai_usage",
            description=(
                "What AI calls have cost, from the ai_usage ledger: totals by role "
                "and by model over a window, plus the most recent calls in full "
                "(tokens in/out/reasoning, latency, cost, the rates used). Filter "
                "by role (e.g. stt.memo, vision.inbox, embed.corpus), kind, caller "
                "or model. A NULL cost is an unpriced model — unknown, never free."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30, "description": "Window, 1–365."},
                    "role": {"type": "string"},
                    "kind": {"type": "string", "enum": ["chat", "embedding", "stt", "tts", "vision", "prediction"]},
                    "caller": {"type": "string", "description": "e.g. integration:transcription"},
                    "model": {"type": "string"},
                    "limit": {"type": "integer", "default": 10, "description": "Recent rows to return, max 100."},
                },
            },
            handler=handle_ai_usage,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "What did that last transcription cost?",
                "How much have we spent on AI this month, and on what?",
                "Show me the vision calls from the last week",
            ],
        ).build(),
        CustomTool(
            name="system_contradictions",
            description=(
                "Check numeric/status claims in prose against measured values. "
                "Two sources: `claude_md` (repo-tracked CLAUDE.md files under "
                "core/ — tool/integration/table counts checked against the "
                "live registry and schema) and `vault_health` (your own vault's "
                "health notes — resting HR/HRV/sleep-average claims checked "
                "against your recorded health data). History (is_history) "
                "chunks are never flagged; stale-but-live chunks are checked "
                "and labelled. Deterministic regex + arithmetic only — no LLM "
                "involved, and it never edits anything."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["claude_md", "vault_health"]},
                        "description": "Which sources to check. Omit for both.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional substring filter on the source path "
                            "(e.g. one CLAUDE.md file, or one vault note)."
                        ),
                    },
                },
            },
            handler=handle_contradictions,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "Is anything in CLAUDE.md out of date?",
                "Check my Health Profile note against my actual health data",
            ],
        ).build(),
        CustomTool(
            name="system_what_lios_sees",
            description=(
                "What does lios see about ME? Per-caller answer, in one call: "
                "every private table you have data in (which integration, a "
                "row count that is always yours alone, earliest/latest "
                "activity, and whether that integration is connected for "
                "you), the household-shared data both of you can always see "
                "(calendar, finance, weather, snags, historical documents, "
                "Home Assistant, the task ledger — each with a one-line "
                "reason it's shared), and a short list of things lios "
                "deliberately does NOT store. Use this to answer 'what do "
                "you know about me' plainly — the summary field is written "
                "to be read aloud."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=handle_what_lios_sees,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "What does lios see about me?",
                "What data do you have on me?",
                "Is my WhatsApp private from the other household user?",
            ],
        ).build(),
        CustomTool(
            name="system_runs",
            description=(
                "Filterable view over the runs ledger (S5.1): every scheduled "
                "job execution (the daily brief pre-warm, the routines tick, "
                "every integration sync, kernel prunes, ...) plus tool calls, "
                "since a given time. `system_alerts`' `recent_runs` is the "
                "same view fixed to the last hour; use this for a longer "
                "window or to filter by name/outcome — e.g. 'did the "
                "routines tick actually run today' or 'what's been failing'. "
                "Scheduled jobs are household-wide; tool calls are scoped to "
                "your own."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "ISO 8601 timestamp. Omit for the last 24h.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Substring filter on the job/tool name.",
                    },
                    "outcome": {
                        "type": "string",
                        "description": (
                            "Filter by outcome. Scheduled-job/manual rows use "
                            "ok/error/skipped, tool-call rows use "
                            "ok/error/timeout — 'skipped' therefore only ever "
                            "matches scheduled jobs."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows returned (default 100, max 500).",
                        "default": 100,
                    },
                },
            },
            handler=handle_runs,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "What ran in the last hour?",
                "Did the routines tick run today?",
                "What's been failing recently?",
            ],
        ).build(),
        CustomTool(
            name="system_alert_log",
            description=(
                "Reviewable log of monitoring alerts (lios#230) — what fired "
                "or cleared since a given time, from Alertmanager's webhook "
                "delivery ledger. Returns `fired` (currently/recently firing, "
                "deduped by fingerprint, newest first, each with "
                "`still_firing`) and `cleared` (resolved since `since`), plus "
                "`counts` split by page == \"phone\" (the ones that were "
                "meant to buzz a handset) vs everything else (FYI). Use this "
                "instead of expecting every alert to have pushed — most "
                "monitoring noise is meant to surface here, at the next "
                "kickoff/check-in, not on the phone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "ISO 8601 timestamp. Required.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows scanned, newest-first (default 200, max 500).",
                        "default": 200,
                    },
                },
                "required": ["since"],
            },
            handler=handle_alert_log,
            annotations=_READ_ONLY,
            category="system",
            examples=[
                "What monitoring alerts fired since yesterday's kickoff?",
                "Has anything cleared since this morning?",
                "Show me what's been flagged, not just what pushed",
            ],
        ).build(),
    ]
