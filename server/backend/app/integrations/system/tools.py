"""MCP tool definitions for system-level diagnostics and composite routines."""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.tokens import OAuthToken, SyncState
from app.models.tool_calls import ToolCall
from app.integrations import get_all

# Thresholds for the tool-call health axis (system_alerts axis 4).
TOOL_FAILURE_THRESHOLD = 3       # >3 error rows for one tool in the lookback window
TOOL_FAILURE_LOOKBACK_MINUTES = 60
TOOL_P95_LOOKBACK_HOURS = 24
TOOL_P95_MIN_CALLS = 10           # don't judge p95 off a handful of calls
TOOL_P95_THRESHOLD_MS = 5000

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Composite tool handlers — aggregate data from multiple integrations
# ---------------------------------------------------------------------------


def handle_morning_briefing(session: Session, arguments: dict[str, Any]) -> str:
    """Aggregate morning overview: calendar, weather, reminders, unread email, health."""
    from app.integrations.google_calendar.tools import handle_today as cal_today
    from app.integrations.weather.tools import handle_current as weather_current
    from app.integrations.weather.tools import handle_forecast as weather_forecast
    from app.integrations.apple_reminders.tools import handle_list_reminders
    from app.integrations.google_mail.tools import handle_unread as gmail_unread
    from app.integrations.apple_health.tools import handle_health_summary

    sections = {}

    # Calendar
    try:
        sections["calendar"] = json.loads(cal_today(session, {}))
    except Exception as e:
        sections["calendar"] = {"error": str(e)}

    # Weather
    try:
        sections["weather_current"] = json.loads(weather_current(session, {}))
    except Exception as e:
        sections["weather_current"] = {"error": str(e)}

    try:
        sections["weather_forecast"] = json.loads(weather_forecast(session, {"days": 1}))
    except Exception as e:
        sections["weather_forecast"] = {"error": str(e)}

    # Reminders (incomplete)
    try:
        sections["reminders"] = json.loads(handle_list_reminders(session, {}))
    except Exception as e:
        sections["reminders"] = {"error": str(e)}

    # Unread email
    try:
        sections["unread_email"] = json.loads(gmail_unread(session, {"limit": 10}))
    except Exception as e:
        sections["unread_email"] = {"error": str(e)}

    # Health
    try:
        sections["health"] = json.loads(handle_health_summary(session, {}))
    except Exception as e:
        sections["health"] = {"error": str(e)}

    # System alerts
    try:
        sections["alerts"] = json.loads(handle_alerts(session, {}))
    except Exception as e:
        sections["alerts"] = {"error": str(e)}

    return json.dumps(sections, indent=2)


def handle_week_ahead(session: Session, arguments: dict[str, Any]) -> str:
    """Aggregate week overview: 7-day calendar, reminders, forecast."""
    from app.integrations.google_calendar.tools import handle_list_events
    from app.integrations.apple_reminders.tools import handle_list_reminders
    from app.integrations.weather.tools import handle_forecast as weather_forecast

    sections = {}

    # Calendar (next 7 days)
    try:
        sections["calendar"] = json.loads(handle_list_events(session, {"days": 7}))
    except Exception as e:
        sections["calendar"] = {"error": str(e)}

    # Reminders (all incomplete)
    try:
        sections["reminders"] = json.loads(handle_list_reminders(session, {}))
    except Exception as e:
        sections["reminders"] = {"error": str(e)}

    # Weather forecast (7 days)
    try:
        sections["forecast"] = json.loads(weather_forecast(session, {"days": 7}))
    except Exception as e:
        sections["forecast"] = {"error": str(e)}

    return json.dumps(sections, indent=2)


def handle_search_everything(session: Session, arguments: dict[str, Any]) -> str:
    """Unified search across vault, email, and WhatsApp using embeddings."""
    query = arguments.get("query", "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    limit = min(int(arguments.get("limit", 5)), 20)

    from app.integrations.obsidian.tools import handle_search as vault_search
    from app.integrations.google_mail.tools import handle_semantic_search as gmail_search
    from app.integrations.whatsapp.tools import handle_semantic_search as wa_search

    sections = {}

    # Vault semantic search
    try:
        sections["vault"] = json.loads(vault_search(session, {"query": query, "limit": limit}))
    except Exception as e:
        sections["vault"] = {"error": str(e)}

    # Gmail semantic search
    try:
        sections["email"] = json.loads(gmail_search(session, {"query": query, "limit": limit}))
    except Exception as e:
        sections["email"] = {"error": str(e)}

    # WhatsApp semantic search
    try:
        sections["whatsapp"] = json.loads(wa_search(session, {"query": query, "limit": limit}))
    except Exception as e:
        sections["whatsapp"] = {"error": str(e)}

    return json.dumps(sections, indent=2)

# Integrations without a sync schedule shouldn't trigger staleness alerts
# (they're on-demand only, e.g. irish_rail, finance)
def _scheduled_integrations() -> set[str]:
    return {name for name, integ in get_all().items() if integ.sync_schedule()}


def handle_alerts(session: Session, arguments: dict[str, Any]) -> str:
    """Return integrations that are failing, stale, or producing no data.

    Three independent axes:
    - SyncState: did the sync job run successfully and recently?
    - Data freshness: is new data actually landing in the table?
    - Bridge heartbeat: is the WhatsApp sidecar's HTTP health endpoint up?

    The first two are independent — a job can succeed while data flow has
    stopped (e.g. when a job is just an embedding chunker, not the producer).
    """
    from app.services.data_freshness import check_all as check_freshness, format_age

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

    # Axis 1: SyncState
    for state in states.values():
        issues = []
        if state.consecutive_failures and state.consecutive_failures >= 1:
            issues.append(f"failing ({state.consecutive_failures}x consecutive)")
        if state.integration in scheduled and state.last_sync_at and state.last_sync_at < cutoff:
            age_mins = int((datetime.now(timezone.utc) - state.last_sync_at).total_seconds() / 60)
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
        entry = {
            "integration": r.integration,
            "latest": r.latest_ts.isoformat() if r.latest_ts else None,
            "age": format_age(r.age_seconds) if r.age_seconds is not None else None,
            "threshold": format_age(r.threshold_seconds),
        }
        freshness.append(entry)
        if r.age_seconds is None:
            _alert(r.integration)["issues"].append("data stale (no records in table)")
        elif r.age_seconds > r.threshold_seconds:
            _alert(r.integration)["issues"].append(
                f"data stale (latest record {format_age(r.age_seconds)} old, "
                f"threshold {format_age(r.threshold_seconds)})"
            )

    # Axis 3: OAuth re-auth required. A flagged token means the integration's
    # sync is permanently failing until the user completes a fresh consent flow
    # — distinct from a transient sync error. Surface as its own block so the
    # daily-note briefing and dashboard banner can render a one-click recovery
    # link instead of generic "21x consecutive" noise.
    reauth_needed = [
        {
            "provider": t.provider,
            "account_email": t.account_email,
            "user_id": t.user_id,
            "flagged_at": t.needs_reauth_at.isoformat() if t.needs_reauth_at else None,
            "reason": t.needs_reauth_reason,
            "reauth_url": f"/api/auth/google/login?account={t.account_email}",
        }
        for t in session.query(OAuthToken).filter(OAuthToken.needs_reauth_at.isnot(None)).all()
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

    alerts = list(alerts_by_integration.values())
    payload = {
        "status": "all_ok" if not alerts and not reauth_needed and not tool_alerts else "degraded",
        "alerts": alerts,
        "data_freshness": freshness,
        "reauth_needed": reauth_needed,
        "tool_alerts": tool_alerts,
    }
    return json.dumps(payload, indent=2 if alerts or reauth_needed or tool_alerts else None)


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions for system diagnostics and composite routines."""
    return [
        {
            "name": "system_alerts",
            "description": (
                "Check for integration health issues. Returns any integrations "
                "that are failing (consecutive errors) or stale (not synced within "
                "the threshold). Use this at the start of daily notes to surface "
                "warnings about data freshness."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "threshold_minutes": {
                        "type": "integer",
                        "description": "Minutes of staleness before alerting (default: 60).",
                        "default": 60,
                    },
                },
            },
            "handler": handle_alerts,
            "category": "system",
            "examples": [
                "Are any integrations broken?",
                "Check system health",
            ],
        },
        {
            "name": "system_morning_briefing",
            "description": (
                "Get a complete morning overview in one call. Returns today's calendar events, "
                "current weather and today's forecast, all incomplete reminders, unread emails, "
                "health summary (steps, sleep, heart rate), and system alerts. "
                "Use this to build a daily note or get a quick start-of-day snapshot."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "handler": handle_morning_briefing,
            "category": "system",
            "examples": [
                "What's my morning look like?",
                "Give me a morning briefing",
                "What do I need to know today?",
            ],
        },
        {
            "name": "system_week_ahead",
            "description": (
                "Get a week-ahead overview: 7 days of calendar events, all incomplete "
                "reminders, and the full weather forecast. Use this for weekly planning "
                "or to see what's coming up."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "handler": handle_week_ahead,
            "category": "system",
            "examples": [
                "What's coming up this week?",
                "Plan my week",
                "What's on for the next few days?",
            ],
        },
        {
            "name": "system_search_everything",
            "description": (
                "Search across vault notes, emails, and WhatsApp messages simultaneously "
                "using semantic (meaning-based) search. Returns the top matches from each "
                "source. Use this when you're looking for something but don't know where it is."
            ),
            "inputSchema": {
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
            "handler": handle_search_everything,
            "category": "search",
            "examples": [
                "Find anything about the renovation budget",
                "Search for Finn's school schedule",
                "What did we say about the plumber?",
            ],
        },
    ]
