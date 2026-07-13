"""Parse Health Auto Export JSON into comar health records.

Handles the JSON format produced by the Health Auto Export iOS app
(https://www.healthyapps.dev/). Converts units, maps workout types,
and generates deterministic UIDs for deduplication.

Used by:
- REST endpoint: /api/health/push (receives JSON from iOS app)
- gRPC: PushHealth already accepts parsed records (client CLI parses locally)
"""

import hashlib
import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

# kJ → kcal conversion factor
KJ_TO_KCAL = 1.0 / 4.184

# Metrics that represent daily totals — multiple entries per day should be summed.
# All other metrics take the last value (point-in-time readings like resting HR).
_SUM_METRICS = {"steps", "distance_km", "active_energy_kcal"}

# Map Health Auto Export workout names to comar workout types
WORKOUT_TYPE_MAP = {
    "Outdoor Walk": "walking",
    "Indoor Walk": "walking",
    "Outdoor Run": "running",
    "Indoor Run": "running",
    "Outdoor Cycle": "cycling",
    "Indoor Cycle": "cycling",
    "Swimming": "swimming",
    "Pool Swim": "swimming",
    "Open Water Swim": "swimming",
    "Hiking": "hiking",
    "Yoga": "yoga",
    "Traditional Strength Training": "strength_training",
    "Functional Strength Training": "strength_training",
    "High Intensity Interval Training": "hiit",
    "Core Training": "core_training",
    "Elliptical": "elliptical",
    "Rowing": "rowing",
    "Stair Climbing": "stair_climbing",
    "Pilates": "pilates",
    "Dance": "dance",
    "Social Dance": "dance",
    "Cooldown": "cooldown",
    "Mixed Cardio": "mixed_cardio",
    "Cross Training": "cross_training",
    "Mind and Body": "mind_and_body",
    "Flexibility": "flexibility",
    "Tennis": "tennis",
    "Golf": "golf",
    "Soccer": "soccer",
    "Basketball": "basketball",
    "Rugby": "rugby",
}


def _parse_export_dt(s: str) -> datetime:
    """Parse Health Auto Export date string like '2026-03-01 00:00:00 +0000'."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")


def _date_str(s: str) -> str:
    """Extract YYYY-MM-DD from a Health Auto Export date string."""
    return _parse_export_dt(s).strftime("%Y-%m-%d")


def _iso(s: str) -> str:
    """Convert Health Auto Export date string to ISO 8601."""
    return _parse_export_dt(s).isoformat()


def _synthetic_uid(prefix: str, *parts: str) -> str:
    """Generate a deterministic UID for records without one.

    Uses a hash so re-importing the same data produces identical UIDs
    and the server's upsert logic deduplicates correctly.
    """
    key = f"{prefix}:{'|'.join(parts)}"
    return f"hae-{hashlib.sha256(key.encode()).hexdigest()[:24]}"


def parse_health_auto_export(raw: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Parse a Health Auto Export JSON payload.

    Accepts the raw JSON dict (either from file or HTTP POST body).
    Returns (daily_metrics, workouts, sleep_sessions) ready for sync_from_push().
    """
    data = raw.get("data", {})
    metrics_list = data.get("metrics", [])
    workouts_list = data.get("workouts", [])

    metrics_by_name = {m["name"]: m for m in metrics_list}

    daily_metrics: list[dict] = []
    sleep_sessions: list[dict] = []

    # --- Daily metrics ---
    _extract_simple_metric(metrics_by_name, "step_count", "steps", 1.0, daily_metrics)
    _extract_simple_metric(metrics_by_name, "walking_running_distance", "distance_km", 1.0, daily_metrics)
    _extract_simple_metric(metrics_by_name, "active_energy", "active_energy_kcal", KJ_TO_KCAL, daily_metrics)
    _extract_simple_metric(metrics_by_name, "resting_heart_rate", "resting_hr_bpm", 1.0, daily_metrics)
    _extract_simple_metric(metrics_by_name, "heart_rate_variability", "hrv_ms", 1.0, daily_metrics)

    # Heart rate has Min/Max/Avg per day
    hr_metric = metrics_by_name.get("heart_rate")
    if hr_metric:
        for entry in hr_metric.get("data", []):
            date_str = _date_str(entry["date"])
            if "Min" in entry:
                daily_metrics.append({"date": date_str, "metric_type": "hr_min_bpm", "value": entry["Min"]})
            if "Avg" in entry:
                daily_metrics.append({"date": date_str, "metric_type": "hr_avg_bpm", "value": entry["Avg"]})
            if "Max" in entry:
                daily_metrics.append({"date": date_str, "metric_type": "hr_max_bpm", "value": entry["Max"]})

    # --- Sleep sessions ---
    sleep_metric = metrics_by_name.get("sleep_analysis")
    if sleep_metric:
        for entry in sleep_metric.get("data", []):
            date_str = _date_str(entry["date"])
            sleep_start = entry.get("sleepStart", entry["date"])
            sleep_end = entry.get("sleepEnd", entry["date"])

            for stage_key, stage_name in [
                ("core", "asleepCore"),
                ("deep", "asleepDeep"),
                ("rem", "asleepREM"),
                ("awake", "awake"),
            ]:
                hours = entry.get(stage_key, 0)
                if hours and hours > 0:
                    sleep_sessions.append({
                        "uid": _synthetic_uid("sleep", date_str, stage_name),
                        "start_time": _iso(sleep_start),
                        "end_time": _iso(sleep_end),
                        "stage": stage_name,
                        "duration_hours": hours,
                    })

    # --- Workouts ---
    workouts: list[dict] = []
    for w in workouts_list:
        workout_name = w.get("name", "Other")
        workout_type = WORKOUT_TYPE_MAP.get(workout_name, f"other_{workout_name.lower().replace(' ', '_')}")

        total_energy_kj = sum(e.get("qty", 0) for e in w.get("activeEnergy", []))

        distance = w.get("distance")
        distance_km = distance.get("qty", 0.0) if isinstance(distance, dict) else 0.0

        avg_hr = w.get("avgHeartRate", {})
        avg_hr_bpm = avg_hr.get("qty", 0.0) if isinstance(avg_hr, dict) else 0.0

        workouts.append({
            "uid": w.get("id", _synthetic_uid("workout", w.get("start", ""), workout_name)),
            "workout_type": workout_type,
            "start_time": _iso(w["start"]),
            "end_time": _iso(w["end"]),
            "duration_seconds": w.get("duration", 0.0),
            "distance_km": distance_km,
            "active_energy_kcal": total_energy_kj * KJ_TO_KCAL,
            "avg_heart_rate_bpm": avg_hr_bpm,
        })

    daily_metrics = _aggregate_daily_metrics(daily_metrics)

    return daily_metrics, workouts, sleep_sessions


def _aggregate_daily_metrics(raw_metrics: list[dict]) -> list[dict]:
    """Aggregate granular entries into one value per (date, metric_type).

    Health Auto Export sends sub-daily data points (e.g. step chunks every
    few minutes). SUM metrics (steps, distance, energy) must be summed to
    get the daily total. Other metrics (HR, HRV) take the last value since
    they're point-in-time readings with one entry per day.
    """
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for m in raw_metrics:
        key = (m["date"], m["metric_type"])
        groups[key].append(m["value"])

    result = []
    for (d, mt), values in groups.items():
        if mt in _SUM_METRICS:
            agg = sum(values)
        else:
            agg = values[-1]
        result.append({"date": d, "metric_type": mt, "value": agg})

    return result


def _extract_simple_metric(
    metrics_by_name: dict,
    export_name: str,
    comar_name: str,
    scale: float,
    out: list[dict],
):
    """Extract a simple qty-based metric from the export data."""
    metric = metrics_by_name.get(export_name)
    if not metric:
        return
    for entry in metric.get("data", []):
        qty = entry.get("qty")
        if qty is not None:
            out.append({
                "date": _date_str(entry["date"]),
                "metric_type": comar_name,
                "value": qty * scale,
            })
