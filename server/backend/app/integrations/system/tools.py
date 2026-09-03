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
from app.models.tool_calls import ToolCall
from app.integrations import get_all
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

    Supersedes `system_morning_briefing` for the `/daily-note` and `/refresh`
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
    )
    # Compact on purpose: indent=2 added 27k characters (130k → 103k measured
    # 2026-09-02) to a payload a model reads, not a person.
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
    from app.models.users import User

    reauth_needed = [
        {
            "provider": t.provider,
            "account_email": t.account_email,
            "user_id": t.user_id,
            "flagged_at": t.needs_reauth_at.isoformat() if t.needs_reauth_at else None,
            "reason": t.needs_reauth_reason,
            # google/login now requires ?user= — thread the owning user's
            # name through so this link doesn't 422.
            "reauth_url": f"/api/auth/google/login?account={t.account_email}&user={u.name}",
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

    # Axis 4: tool-call health, from the tool_calls audit trail (per-call
    # dispatch log in app/mcp/server.py + app/api/v1.py). Independent of the
    # sync-based axes above — a tool can be broken (bad args handling, a
    # downstream API returning garbage) while its integration's own sync job
    # is perfectly healthy.
    tool_alerts: list[dict] = []

    failure_cutoff = datetime.now(timezone.utc) - timedelta(minutes=TOOL_FAILURE_LOOKBACK_MINUTES)
    failing_tools = (
        session.query(ToolCall.name, func.count(ToolCall.id).label("failures"))
        .filter(ToolCall.status == "error", ToolCall.called_at >= failure_cutoff)
        .group_by(ToolCall.name)
        .having(func.count(ToolCall.id) > TOOL_FAILURE_THRESHOLD)
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
    p95_expr = func.percentile_cont(0.95).within_group(ToolCall.duration_ms.asc())
    slow_tools = (
        session.query(
            ToolCall.name,
            p95_expr.label("p95_duration_ms"),
            func.count(ToolCall.id).label("calls"),
        )
        .filter(ToolCall.called_at >= p95_cutoff)
        .group_by(ToolCall.name)
        .having(func.count(ToolCall.id) >= TOOL_P95_MIN_CALLS)
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
    for token in daemon_tokens:
        label = token.label or f"token-{token.id}"

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
    }


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
                "that list has never been checked, not confirmed healthy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threshold_minutes": {
                        "type": "integer",
                        "description": "Minutes of staleness before alerting (default: 60).",
                        "default": 60,
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
                            "ISO timestamp of the previous daily note, used to report "
                            "what changed in reminders since then. Omit for a 24h default."
                        ),
                    },
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Fetch only the sources feeding these sections (e.g. "
                            "[\"email\", \"transport\"]). Omit for everything. System "
                            "alerts are always included."
                        ),
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": "Bypass the cache and re-fetch every source.",
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
    ]
