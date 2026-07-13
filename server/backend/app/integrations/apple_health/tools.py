"""MCP tool definitions for Apple Health integration."""

import json
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.integrations.apple_health.models import (
    HealthDailyMetric,
    HealthSleepSession,
    HealthWorkout,
)
from app.tools.helpers import scoped_query

logger = logging.getLogger(__name__)


def _today() -> date:
    return date.today()


def handle_health_today(session: Session, arguments: dict) -> str:
    """Return today's health metrics."""
    d = arguments.get("date")
    target = date.fromisoformat(d) if d else _today()

    rows = (
        scoped_query(session, HealthDailyMetric)
        .filter(HealthDailyMetric.date == target)
        .all()
    )

    metrics = {r.metric_type: round(r.value, 1) for r in rows}
    return json.dumps({
        "date": target.isoformat(),
        "metrics": metrics,
    })


def handle_health_sleep(session: Session, arguments: dict) -> str:
    """Return sleep data for last night (or specified date)."""
    d = arguments.get("date")
    target = date.fromisoformat(d) if d else _today()

    # A night "belongs" to the date its sleep ends on. Bucket by end_time only
    # (within a noon-to-3pm window around that date) — filtering on start_time
    # too widens the window past 24h and pulls in the *next* night's session
    # as well, double-counting hours across two nights.
    start_of_day = datetime(target.year, target.month, target.day, tzinfo=timezone.utc)
    prev_evening = start_of_day - timedelta(hours=12)
    next_afternoon = start_of_day + timedelta(hours=15)

    rows = (
        scoped_query(session, HealthSleepSession)
        .filter(
            HealthSleepSession.end_time >= prev_evening,
            HealthSleepSession.end_time < next_afternoon,
        )
        .order_by(HealthSleepSession.start_time)
        .all()
    )

    sessions = []
    total_hours = 0.0
    stage_totals = {}
    for r in rows:
        sessions.append({
            "stage": r.stage,
            "start": r.start_time.isoformat() if r.start_time else None,
            "end": r.end_time.isoformat() if r.end_time else None,
            "hours": round(r.duration_hours, 2),
        })
        total_hours += r.duration_hours
        stage_totals[r.stage] = stage_totals.get(r.stage, 0.0) + r.duration_hours

    return json.dumps({
        "date": target.isoformat(),
        "total_hours": round(total_hours, 2),
        "stage_breakdown": {k: round(v, 2) for k, v in stage_totals.items()},
        "sessions": sessions,
    })


def handle_health_workouts(session: Session, arguments: dict) -> str:
    """Return recent workouts."""
    days = arguments.get("days", 7)
    workout_type = arguments.get("type")
    since = datetime.now(timezone.utc) - timedelta(days=days)

    query = (
        scoped_query(session, HealthWorkout)
        .filter(HealthWorkout.start_time >= since)
    )
    if workout_type:
        query = query.filter(HealthWorkout.workout_type == workout_type)

    rows = query.order_by(HealthWorkout.start_time.desc()).all()

    workouts = []
    for r in rows:
        workouts.append({
            "type": r.workout_type,
            "date": r.start_time.strftime("%Y-%m-%d") if r.start_time else None,
            "start": r.start_time.isoformat() if r.start_time else None,
            "duration_min": round(r.duration_seconds / 60, 1),
            "distance_km": round(r.distance_km, 2) if r.distance_km else 0,
            "energy_kcal": round(r.active_energy_kcal, 0) if r.active_energy_kcal else 0,
            "avg_hr_bpm": round(r.avg_heart_rate_bpm, 0) if r.avg_heart_rate_bpm else 0,
        })

    return json.dumps({"days": days, "count": len(workouts), "workouts": workouts})


def handle_health_trends(session: Session, arguments: dict) -> str:
    """Return daily metric trends over a period."""
    days = arguments.get("days", 7)
    metric = arguments.get("metric")  # optional filter to single metric
    since = _today() - timedelta(days=days - 1)

    query = scoped_query(session, HealthDailyMetric).filter(HealthDailyMetric.date >= since)
    if metric:
        query = query.filter(HealthDailyMetric.metric_type == metric)

    rows = query.order_by(HealthDailyMetric.date).all()

    # Group by date
    by_date: dict[str, dict] = {}
    for r in rows:
        d = r.date.isoformat()
        if d not in by_date:
            by_date[d] = {}
        by_date[d][r.metric_type] = round(r.value, 1)

    # Compute averages per metric across the period
    metric_sums: dict[str, list[float]] = {}
    for r in rows:
        metric_sums.setdefault(r.metric_type, []).append(r.value)

    averages = {
        k: round(sum(v) / len(v), 1) for k, v in metric_sums.items()
    }

    return json.dumps({
        "days": days,
        "from": since.isoformat(),
        "to": _today().isoformat(),
        "daily": by_date,
        "period_averages": averages,
    })


def handle_health_summary(session: Session, arguments: dict) -> str:
    """Pre-formatted health summary for daily notes."""
    today = _today()
    yesterday = today - timedelta(days=1)

    # Today's metrics
    today_rows = (
        scoped_query(session, HealthDailyMetric)
        .filter(HealthDailyMetric.date == today)
        .all()
    )
    today_metrics = {r.metric_type: r.value for r in today_rows}

    # Yesterday's metrics (for comparison)
    yesterday_rows = (
        scoped_query(session, HealthDailyMetric)
        .filter(HealthDailyMetric.date == yesterday)
        .all()
    )
    yesterday_metrics = {r.metric_type: r.value for r in yesterday_rows}

    # Last night's sleep — bucket by end_time only (see handle_health_sleep)
    # so we don't pull in tonight's not-yet-finished session too.
    start_of_today = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    prev_evening = start_of_today - timedelta(hours=12)
    next_afternoon = start_of_today + timedelta(hours=15)

    sleep_rows = (
        scoped_query(session, HealthSleepSession)
        .filter(
            HealthSleepSession.end_time >= prev_evening,
            HealthSleepSession.end_time < next_afternoon,
        )
        .all()
    )
    sleep_total = sum(r.duration_hours for r in sleep_rows)
    sleep_stages = {}
    for r in sleep_rows:
        sleep_stages[r.stage] = sleep_stages.get(r.stage, 0.0) + r.duration_hours

    # Recent workouts (last 2 days)
    recent_workouts = (
        scoped_query(session, HealthWorkout)
        .filter(HealthWorkout.start_time >= start_of_today - timedelta(days=2))
        .order_by(HealthWorkout.start_time.desc())
        .limit(3)
        .all()
    )

    # Build summary
    lines = []

    # Steps
    steps = today_metrics.get("steps")
    y_steps = yesterday_metrics.get("steps")
    if steps is not None:
        step_line = f"Steps: {int(steps):,}"
        if y_steps:
            step_line += f" (yesterday: {int(y_steps):,})"
        lines.append(step_line)

    # Distance
    dist = today_metrics.get("distance_km")
    if dist:
        lines.append(f"Distance: {dist:.1f} km")

    # Active energy
    energy = today_metrics.get("active_energy_kcal")
    if energy:
        lines.append(f"Active energy: {int(energy)} kcal")

    # Heart rate
    hr_avg = today_metrics.get("hr_avg_bpm") or yesterday_metrics.get("hr_avg_bpm")
    resting_hr = today_metrics.get("resting_hr_bpm") or yesterday_metrics.get("resting_hr_bpm")
    hrv = today_metrics.get("hrv_ms") or yesterday_metrics.get("hrv_ms")

    hr_parts = []
    if resting_hr:
        hr_parts.append(f"resting {int(resting_hr)}")
    if hr_avg:
        hr_min = today_metrics.get("hr_min_bpm") or yesterday_metrics.get("hr_min_bpm")
        hr_max = today_metrics.get("hr_max_bpm") or yesterday_metrics.get("hr_max_bpm")
        if hr_min and hr_max:
            hr_parts.append(f"range {int(hr_min)}–{int(hr_max)}")
        else:
            hr_parts.append(f"avg {int(hr_avg)}")
    if hrv:
        hr_parts.append(f"HRV {int(hrv)} ms")
    if hr_parts:
        lines.append(f"Heart rate: {', '.join(hr_parts)} bpm")

    # Sleep
    if sleep_total > 0:
        h = int(sleep_total)
        m = int((sleep_total - h) * 60)
        sleep_line = f"Sleep: {h}h{m:02d}m"
        # Add stage breakdown if available
        deep = sleep_stages.get("asleepDeep", 0)
        rem = sleep_stages.get("asleepREM", 0)
        if deep or rem:
            parts = []
            if deep:
                parts.append(f"{deep:.1f}h deep")
            if rem:
                parts.append(f"{rem:.1f}h REM")
            sleep_line += f" ({', '.join(parts)})"
        lines.append(sleep_line)

    # Workouts
    for w in recent_workouts:
        dur = int(w.duration_seconds / 60)
        w_line = f"Workout: {w.workout_type} — {dur} min"
        if w.distance_km:
            w_line += f", {w.distance_km:.1f} km"
        if w.avg_heart_rate_bpm:
            w_line += f", avg HR {int(w.avg_heart_rate_bpm)}"
        lines.append(w_line)

    return json.dumps({
        "date": today.isoformat(),
        "summary": "\n".join(lines) if lines else "No health data yet today.",
    })


# Workout types that count as "strength & conditioning" for weekly targets
STRENGTH_TYPES = {"strength_training", "hiit", "cross_training", "core_training"}


def handle_health_exercise_status(session: Session, arguments: dict) -> str:
    """Return exercise adherence against weekly targets."""
    week_of = arguments.get("week_of")
    if week_of:
        ref = date.fromisoformat(week_of)
    else:
        ref = _today()

    # Week runs Monday to Sunday
    week_start = ref - timedelta(days=ref.weekday())  # Monday
    week_end = week_start + timedelta(days=6)  # Sunday

    since = datetime(
        week_start.year, week_start.month, week_start.day, tzinfo=timezone.utc
    )
    until = datetime(
        week_end.year, week_end.month, week_end.day, 23, 59, 59, tzinfo=timezone.utc
    )

    rows = (
        scoped_query(session, HealthWorkout)
        .filter(
            HealthWorkout.start_time >= since,
            HealthWorkout.start_time <= until,
        )
        .order_by(HealthWorkout.start_time)
        .all()
    )

    workouts = []
    strength_count = 0
    for r in rows:
        w = {
            "type": r.workout_type,
            "date": r.start_time.strftime("%Y-%m-%d") if r.start_time else None,
            "duration_min": round(r.duration_seconds / 60, 1),
            "energy_kcal": round(r.active_energy_kcal, 0) if r.active_energy_kcal else 0,
            "avg_hr_bpm": round(r.avg_heart_rate_bpm, 0) if r.avg_heart_rate_bpm else 0,
        }
        workouts.append(w)
        if r.workout_type in STRENGTH_TYPES:
            strength_count += 1

    target_strength = 2

    # Remaining days in the week (from today, not from ref)
    today = _today()
    remaining = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        if d > today and d <= week_end:
            remaining.append(d.isoformat())

    on_track = strength_count >= target_strength or (
        strength_count + len(remaining) >= target_strength
    )

    return json.dumps({
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "target": {"strength_sessions": target_strength},
        "actual": {
            "strength_sessions": strength_count,
            "total_workouts": len(workouts),
            "workouts": workouts,
        },
        "remaining_days": remaining,
        "on_track": on_track,
    })


def handle_health_weekly_summary(session: Session, arguments: dict) -> str:
    """Aggregated weekly health data for the weekly review."""
    week_start_str = arguments.get("week_start")
    if week_start_str:
        week_start = date.fromisoformat(week_start_str)
    else:
        today = _today()
        week_start = today - timedelta(days=today.weekday())

    week_end = week_start + timedelta(days=6)

    # --- Sleep ---
    sleep_start = datetime(
        week_start.year, week_start.month, week_start.day, tzinfo=timezone.utc
    ) - timedelta(hours=12)
    sleep_end = datetime(
        week_end.year, week_end.month, week_end.day, 23, 59, 59, tzinfo=timezone.utc
    ) + timedelta(days=1)

    sleep_rows = (
        scoped_query(session, HealthSleepSession)
        .filter(
            HealthSleepSession.end_time >= sleep_start,
            HealthSleepSession.start_time < sleep_end,
        )
        .all()
    )

    # Group sleep by night (date the sleep ends on)
    nights: dict[str, dict[str, float]] = {}
    for r in sleep_rows:
        if not r.end_time:
            continue
        night = r.end_time.strftime("%Y-%m-%d")
        if night not in nights:
            nights[night] = {"total": 0.0, "deep": 0.0, "rem": 0.0}
        nights[night]["total"] += r.duration_hours
        if r.stage == "asleepDeep":
            nights[night]["deep"] += r.duration_hours
        elif r.stage == "asleepREM":
            nights[night]["rem"] += r.duration_hours

    sleep_data = {}
    if nights:
        totals = [v["total"] for v in nights.values()]
        deeps = [v["deep"] for v in nights.values()]
        rems = [v["rem"] for v in nights.values()]
        best = max(nights, key=lambda k: nights[k]["total"])
        worst = min(nights, key=lambda k: nights[k]["total"])
        sleep_data = {
            "nights_tracked": len(nights),
            "avg_total_hours": round(sum(totals) / len(totals), 1),
            "avg_deep_hours": round(sum(deeps) / len(deeps), 1),
            "avg_rem_hours": round(sum(rems) / len(rems), 1),
            "best_night": best,
            "best_night_hours": round(nights[best]["total"], 1),
            "worst_night": worst,
            "worst_night_hours": round(nights[worst]["total"], 1),
        }

    # --- Vitals (daily metrics) ---
    metric_rows = (
        scoped_query(session, HealthDailyMetric)
        .filter(
            HealthDailyMetric.date >= week_start,
            HealthDailyMetric.date <= week_end,
        )
        .all()
    )

    metrics_by_type: dict[str, list[float]] = {}
    for r in metric_rows:
        metrics_by_type.setdefault(r.metric_type, []).append(r.value)

    def avg(key: str) -> float | None:
        vals = metrics_by_type.get(key)
        return round(sum(vals) / len(vals), 1) if vals else None

    vitals_data = {
        "avg_resting_hr": avg("resting_hr_bpm"),
        "avg_hrv": avg("hrv_ms"),
    }

    # HRV trend: compare first half vs second half of the week
    hrv_vals = metrics_by_type.get("hrv_ms", [])
    if len(hrv_vals) >= 4:
        mid = len(hrv_vals) // 2
        first_half = sum(hrv_vals[:mid]) / mid
        second_half = sum(hrv_vals[mid:]) / (len(hrv_vals) - mid)
        if second_half > first_half * 1.1:
            vitals_data["hrv_trend"] = "improving"
        elif second_half < first_half * 0.9:
            vitals_data["hrv_trend"] = "declining"
        else:
            vitals_data["hrv_trend"] = "stable"

    # --- Movement ---
    movement_data = {
        "avg_steps": avg("steps"),
        "total_distance_km": round(sum(metrics_by_type.get("distance_km", [])), 1),
        "avg_active_energy": avg("active_energy_kcal"),
    }

    # --- Exercise ---
    workout_since = datetime(
        week_start.year, week_start.month, week_start.day, tzinfo=timezone.utc
    )
    workout_until = datetime(
        week_end.year, week_end.month, week_end.day, 23, 59, 59, tzinfo=timezone.utc
    )

    workout_rows = (
        scoped_query(session, HealthWorkout)
        .filter(
            HealthWorkout.start_time >= workout_since,
            HealthWorkout.start_time <= workout_until,
        )
        .order_by(HealthWorkout.start_time)
        .all()
    )

    workouts = []
    strength_count = 0
    for r in workout_rows:
        workouts.append({
            "type": r.workout_type,
            "date": r.start_time.strftime("%Y-%m-%d") if r.start_time else None,
            "duration_min": round(r.duration_seconds / 60, 1),
        })
        if r.workout_type in STRENGTH_TYPES:
            strength_count += 1

    exercise_data = {
        "total_workouts": len(workouts),
        "strength_sessions": strength_count,
        "target_met": strength_count >= 2,
        "workouts": workouts,
    }

    # --- Recovery assessment ---
    avg_sleep = sleep_data.get("avg_total_hours", 0)
    hrv_trend = vitals_data.get("hrv_trend", "stable")

    if avg_sleep >= 7 and hrv_trend == "improving":
        recovery = "good"
    elif avg_sleep < 6 or hrv_trend == "declining":
        recovery = "poor"
    else:
        recovery = "fair"

    return json.dumps({
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "sleep": sleep_data,
        "vitals": vitals_data,
        "movement": movement_data,
        "exercise": exercise_data,
        "recovery_assessment": recovery,
    })


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions for Apple Health."""
    return [
        {
            "name": "health_today",
            "description": (
                "Today's health metrics: steps, distance, active energy, heart rate "
                "(resting, avg, min, max), and HRV. Pass 'date' for a specific day."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format (default: today)",
                    },
                },
            },
            "handler": handle_health_today,
            "category": "health",
            "examples": [
                "How many steps today?",
                "What's my heart rate?",
            ],
        },
        {
            "name": "health_sleep",
            "description": (
                "Last night's sleep breakdown by stage (deep, REM, core, awake) with "
                "total hours and individual session times. Pass 'date' for a specific night."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD (sleep ending on this date). Default: today.",
                    },
                },
            },
            "handler": handle_health_sleep,
            "category": "health",
            "examples": [
                "How did I sleep last night?",
                "How much deep sleep did I get?",
            ],
        },
        {
            "name": "health_workouts",
            "description": (
                "Recent workouts with type, duration, distance, calories burned, and "
                "average heart rate. Filter by workout type (e.g. running, cycling) "
                "and number of days to look back."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back (default: 7).",
                    },
                    "type": {
                        "type": "string",
                        "description": "Filter to workout type (e.g. 'running', 'cycling').",
                    },
                },
            },
            "handler": handle_health_workouts,
            "category": "health",
            "examples": [
                "What workouts did I do this week?",
                "Show my recent runs",
            ],
        },
        {
            "name": "health_trends",
            "description": (
                "Daily health metric trends over a period with period averages. "
                "Shows day-by-day values for steps, heart rate, HRV, etc. "
                "Great for weekly reviews and spotting trends."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to include (default: 7).",
                    },
                    "metric": {
                        "type": "string",
                        "description": "Filter to single metric type (e.g. 'steps', 'hrv_ms').",
                    },
                },
            },
            "handler": handle_health_trends,
            "category": "health",
            "examples": [
                "How are my steps trending?",
                "Show health trends for the last week",
                "Is my HRV improving?",
            ],
        },
        {
            "name": "health_summary",
            "description": (
                "Pre-formatted health snapshot for daily notes. Combines today's steps, "
                "distance, active energy, heart rate, last night's sleep (with stage breakdown), "
                "and recent workouts into a concise text summary."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "handler": handle_health_summary,
            "category": "health",
            "examples": [
                "Health summary for the daily note",
                "How am I doing health-wise?",
            ],
        },
        {
            "name": "health_exercise_status",
            "description": (
                "Weekly exercise adherence — how many strength & conditioning sessions "
                "completed vs the 2/week target, with workout details and remaining days. "
                "Shows whether on track for the week."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "week_of": {
                        "type": "string",
                        "description": (
                            "Any date within the target week (YYYY-MM-DD). "
                            "Default: current week."
                        ),
                    },
                },
            },
            "handler": handle_health_exercise_status,
            "category": "health",
            "examples": [
                "Am I on track for my workout target?",
                "How many strength sessions this week?",
            ],
        },
        {
            "name": "health_weekly_summary",
            "description": (
                "Aggregated weekly health data for the weekly review. Returns sleep averages "
                "(total, deep, REM, best/worst night), vitals (resting HR, HRV with trend), "
                "movement (steps, distance, energy), exercise adherence, and a recovery assessment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "week_start": {
                        "type": "string",
                        "description": (
                            "Monday of the target week (YYYY-MM-DD). "
                            "Default: current week's Monday."
                        ),
                    },
                },
            },
            "handler": handle_health_weekly_summary,
            "category": "health",
            "examples": [
                "Weekly health summary for the review",
                "How was my health this week?",
            ],
        },
    ]
