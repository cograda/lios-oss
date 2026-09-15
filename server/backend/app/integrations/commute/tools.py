"""MCP tool definitions and handlers for the commute solver.

`commute_query` is the primary surface: it hits the live feeds directly and
answers "when do I leave?" at any hour on any day. `commute_status` and
`commute_history` read Postgres rows written by the scheduled weekday-morning
job, and exist for auditing and buffer-tuning respectively — `commute_status`
is deliberately NOT the answer tool, since it can only ever be as fresh as the
last scheduled run.
"""

import dataclasses
import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.plugin.config_store import plugin_config
from app.integrations.commute.domain import ArriveBy, DEFAULT, DepartAfter
from app.integrations.commute.routing import routes
from app.integrations.commute.models import CommuteDecision
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import age_seconds, iso_or_none, serialize

logger = logging.getLogger(__name__)

# The weekday-morning window the solver actually runs in (Europe/Dublin local
# time). Outside it, "no fresh decision" is expected, not a data problem.
WINDOW_START = time(7, 0)
WINDOW_END = time(9, 0)
STALE_AFTER_MIN = 3


def _dublin_now() -> datetime:
    from app.integrations.commute.client import dublin_now

    return dublin_now()


def _window_active(now: datetime) -> bool:
    return now.weekday() < 5 and WINDOW_START <= now.time() <= WINDOW_END


DECISION_FIELDS = [
    "decided_at", "state", "status_text", "leave_in_min", "confidence",
    "degraded", "reason", "target_bus_trip_id", "target_bus_route",
    "target_bus_dep_home", "target_bus_arr_interchange", "target_train_code",
    "target_train_interchange_dep", "target_train_dest_arr", "interchange_delay_min",
    "bus_feed_ts", "dart_feed_ts", "bus_count", "dart_count", "ha_pushed",
]
_TS_TRANSFORMS = {
    f: iso_or_none for f in (
        "decided_at", "target_bus_dep_home", "target_bus_arr_interchange",
        "target_train_interchange_dep", "target_train_dest_arr",
        "bus_feed_ts", "dart_feed_ts",
    )
}


def handle_status(session: Session, arguments: dict[str, Any]) -> str:
    """Latest commute decision, with window/freshness context."""
    now = _dublin_now()
    window_active = _window_active(now)

    latest = session.query(CommuteDecision).order_by(CommuteDecision.decided_at.desc()).first()
    if latest is None:
        return json.dumps({
            "window_active": window_active,
            "message": "No commute decisions recorded yet.",
        })

    age = age_seconds(latest.decided_at)
    stale = window_active and age is not None and age > STALE_AFTER_MIN * 60

    return json.dumps({
        "window_active": window_active,
        "stale": stale,
        "decision": serialize(latest, DECISION_FIELDS, transforms=_TS_TRANSFORMS),
    }, indent=2)


def handle_history(session: Session, arguments: dict[str, Any]) -> str:
    """Recent decision counts + interchange delay distribution, for tuning interchange_buffer_min."""
    days = min(int(arguments.get("days", 14)), 90)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        session.query(CommuteDecision)
        .filter(CommuteDecision.decided_at >= since)
        .order_by(CommuteDecision.decided_at.asc())
        .all()
    )

    counts_by_state: dict[str, int] = {}
    # Dedupe to the last sample per (date, trip_id) — the sync runs roughly
    # once a minute, so the same bus otherwise contributes many near-identical
    # delay samples.
    latest_per_trip: dict[tuple, CommuteDecision] = {}
    for row in rows:
        counts_by_state[row.state] = counts_by_state.get(row.state, 0) + 1
        if row.target_bus_trip_id and row.interchange_delay_min is not None:
            key = (row.decided_at.date(), row.target_bus_trip_id)
            latest_per_trip[key] = row

    delays = sorted(row.interchange_delay_min for row in latest_per_trip.values())
    delay_stats = None
    if delays:
        n = len(delays)
        delay_stats = {
            "n": n,
            "mean": round(sum(delays) / n, 2),
            "p50": round(delays[n // 2], 2),
            "p90": round(delays[min(n - 1, int(n * 0.9))], 2),
            "max": round(delays[-1], 2),
        }

    return json.dumps({
        "days": days,
        "decision_count": len(rows),
        "counts_by_state": counts_by_state,
        "interchange_delay_distribution_min": delay_stats,
    }, indent=2)


def _parse_hhmm(s: str) -> time:
    h, m = (int(x) for x in s.split(":"))
    return time(h, m)


def handle_query(session: Session, arguments: dict[str, Any]) -> str:
    """On-demand commute query — live feeds, any direction/objective, never
    persisted (that's what the scheduled morning job + commute_status/history
    are for) and never masked to "logging only" (the user explicitly asked)."""
    from app.integrations.commute.client import dublin_now, fetch_bus_departures, fetch_dart_services
    from app.integrations.commute.solver import solve, solve_dart_only, solve_return

    route_name = arguments.get("route", "outbound")
    all_routes = routes()
    route = all_routes.get(route_name)
    if route is None:
        return json.dumps({"error": f"Unknown route {route_name!r}. Choose one of {sorted(all_routes)}."})

    objective_type = arguments.get("objective", "depart_now")
    time_str = arguments.get("time")
    now = dublin_now()

    if objective_type == "arrive_by":
        if not time_str:
            return json.dumps({"error": "time (HH:MM) is required for objective=arrive_by"})
        objective = ArriveBy(deadline=_parse_hhmm(time_str))
    elif objective_type == "depart_after":
        if not time_str:
            return json.dumps({"error": "time (HH:MM) is required for objective=depart_after"})
        objective = DepartAfter(earliest=_parse_hhmm(time_str))
    elif objective_type == "depart_now":
        objective = DepartAfter(earliest=now.time())
    else:
        return json.dumps({"error": f"Unknown objective {objective_type!r}."})

    commute_cfg = plugin_config("commute")
    cfg = dataclasses.replace(DEFAULT, interchange_buffer_min=commute_cfg.commute_interchange_buffer_min)

    darts, dart_ts = fetch_dart_services(now, route)

    if route.bus_alight_stop is None:
        decision = solve_dart_only(darts, now, objective, cfg, dart_feed_ts=dart_ts)
        bus_count = None
    elif route.name == "outbound":
        buses, bus_ts = fetch_bus_departures(commute_cfg.nta_api_key, now, route, cfg)
        decision = solve(buses, darts, now, objective, cfg, bus_feed_ts=bus_ts, dart_feed_ts=dart_ts)
        bus_count = len(buses)
    else:
        # return: board the DART first, catch the connecting bus home —
        # solve_return() expects legs in that chronological order.
        buses, bus_ts = fetch_bus_departures(commute_cfg.nta_api_key, now, route, cfg)
        decision = solve_return(darts, buses, now, objective, cfg, dart_feed_ts=dart_ts, bus_feed_ts=bus_ts)
        bus_count = len(buses)

    return json.dumps({
        "route": route.name,
        "objective": objective_type,
        "bus_leg_configured": route.bus_alight_stop is not None,
        "state": decision.state,
        "status_text": decision.status_text,
        "leave_in_min": decision.leave_in_min,
        "confidence": decision.confidence,
        "degraded": decision.degraded,
        "reason": decision.reason or None,
        "target_bus": (
            {"trip_id": decision.target_bus.trip_id, "route": decision.target_bus.route,
             "depart": decision.target_bus.depart.isoformat(), "arrive": decision.target_bus.arrive.isoformat()}
            if decision.target_bus else None
        ),
        "target_train": (
            {"traincode": decision.target_train.traincode, "destination": decision.target_train.destination,
             "depart": decision.target_train.depart.isoformat(), "arrive": decision.target_train.arrive.isoformat()}
            if decision.target_train else None
        ),
        "bus_count": bus_count,
        "dart_count": len(darts),
    }, indent=2)


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        CustomTool(
            name="commute_status",
            description=(
                "The last decision RECORDED BY THE SCHEDULED weekday-morning job — "
                "an audit/history record, not a live answer. The job only runs "
                "07:00-08:57 on weekdays, so outside that window this returns a "
                "stale row by design (check window_active). "
                "DO NOT use this to answer 'when should I leave?' — use "
                "commute_query, which is live and works at any hour on any day. "
                "Reach for this only to inspect what the scheduled job decided."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_status,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="home",
            examples=[
                "What did the morning commute job decide today?",
                "Did the scheduled solver run this morning?",
            ],
        ).build(),
        CustomTool(
            name="commute_history",
            description=(
                "Recent commute decision counts by state and the interchange "
                "delay distribution — use this to tune the interchange_buffer_min config "
                "on evidence rather than a guess."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Look-back window in days (default 14, max 90).",
                        "default": 14,
                    },
                },
            },
            handler=handle_history,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="home",
            examples=[
                "How reliable has the bus-to-rail connection been?",
                "Should I change the commute buffer?",
            ],
        ).build(),
        CustomTool(
            name="commute_query",
            description=(
                "THE tool for 'when should I leave?' — the live bus->train "
                "connection answer, working at ANY hour on ANY day (no window, no "
                "schedule). Returns leave_in_min plus the specific target bus and "
                "train. Default objective 'depart_now' answers 'when do I leave for "
                "the next connection?', which is the common case — call it with no "
                "arguments for that. "
                "route: 'outbound' (home -> interchange -> destination) or "
                "'return' (destination -> interchange -> home; the return bus leg may "
                "not be configured yet — check bus_leg_configured, it still answers "
                "with the next DART if not). objective: 'arrive_by' (latest bus that "
                "still makes a deadline — requires time), 'depart_after' (earliest "
                "connection at/after a time — requires time), or 'depart_now' "
                "(fastest connection leaving right now, default). "
                "Live and uncached, so it is never stale; deliberately not written to "
                "history (commute_status is the scheduled job's audit record)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "enum": ["outbound", "return"],
                        "description": "Direction: 'outbound' (home->Dublin) or 'return' (Dublin->home). Default outbound.",
                        "default": "outbound",
                    },
                    "objective": {
                        "type": "string",
                        "enum": ["arrive_by", "depart_after", "depart_now"],
                        "description": "What to optimize for. Default depart_now.",
                        "default": "depart_now",
                    },
                    "time": {
                        "type": "string",
                        "description": "HH:MM, required for objective=arrive_by or depart_after.",
                    },
                },
            },
            handler=handle_query,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True),
            category="home",
            examples=[
                "When should I leave for the train?",
                "When do I leave for the next bus to train connection?",
                "What's my commute status?",
                "I need to be in the office by 10:30, when should I leave?",
                "I'm leaving the house around 9, what's my connection?",
                "I'm leaving the office now, what's the fastest way home?",
            ],
        ).build(),
    ]
