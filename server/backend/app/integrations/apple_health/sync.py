"""Sync Apple Health data from Mac agent push into Postgres."""

import logging
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.integrations.apple_health.models import (
    HealthDailyMetric,
    HealthSleepSession,
    HealthWorkout,
)

logger = logging.getLogger(__name__)


def sync_from_push(
    daily_metrics: list[dict],
    workouts: list[dict],
    sleep_sessions: list[dict],
    session: Session,
    *,
    user_id: int,
) -> int:
    """Upsert health data received from the Mac agent.

    user_id is now resolved at the auth boundary (push endpoint pulls it from
    the bearer token). The string `user` form is gone — see Phase A of
    multi-user-e2e for the migration.

    Returns total number of records processed.
    """
    now = datetime.now(timezone.utc)
    count = 0

    # --- Daily metrics: upsert on (user_id, date, metric_type) ---
    for item in daily_metrics:
        d = item.get("date")
        metric_type = item.get("metric_type", "")
        value = item.get("value", 0.0)

        if not d or not metric_type:
            continue

        if isinstance(d, str):
            d = date.fromisoformat(d)

        existing = (
            session.query(HealthDailyMetric)
            .filter_by(user_id=user_id, date=d, metric_type=metric_type)
            .first()
        )
        if existing:
            existing.value = value
            existing.synced_at = now
        else:
            session.add(HealthDailyMetric(
                user_id=user_id,
                date=d,
                metric_type=metric_type,
                value=value,
                synced_at=now,
            ))
            session.flush()
        count += 1

    # --- Workouts: upsert on (user_id, uid) ---
    for item in workouts:
        uid = item.get("uid", "").strip()
        if not uid:
            continue

        existing = (
            session.query(HealthWorkout)
            .filter_by(user_id=user_id, uid=uid)
            .first()
        )

        start_time = _parse_dt(item.get("start_time"))
        end_time = _parse_dt(item.get("end_time"))

        if existing:
            existing.workout_type = item.get("workout_type", existing.workout_type)
            existing.start_time = start_time or existing.start_time
            existing.end_time = end_time or existing.end_time
            existing.duration_seconds = item.get("duration_seconds", existing.duration_seconds)
            existing.distance_km = item.get("distance_km", existing.distance_km)
            existing.active_energy_kcal = item.get("active_energy_kcal", existing.active_energy_kcal)
            existing.avg_heart_rate_bpm = item.get("avg_heart_rate_bpm", existing.avg_heart_rate_bpm)
            existing.synced_at = now
        else:
            session.add(HealthWorkout(
                uid=uid,
                user_id=user_id,
                workout_type=item.get("workout_type", "other"),
                start_time=start_time,
                end_time=end_time,
                duration_seconds=item.get("duration_seconds", 0.0),
                distance_km=item.get("distance_km", 0.0),
                active_energy_kcal=item.get("active_energy_kcal", 0.0),
                avg_heart_rate_bpm=item.get("avg_heart_rate_bpm", 0.0),
                synced_at=now,
            ))
        count += 1

    # --- Sleep sessions: upsert on (user_id, uid) ---
    for item in sleep_sessions:
        uid = item.get("uid", "").strip()
        if not uid:
            continue

        existing = (
            session.query(HealthSleepSession)
            .filter_by(user_id=user_id, uid=uid)
            .first()
        )

        start_time = _parse_dt(item.get("start_time"))
        end_time = _parse_dt(item.get("end_time"))

        if existing:
            existing.start_time = start_time or existing.start_time
            existing.end_time = end_time or existing.end_time
            existing.stage = item.get("stage", existing.stage)
            existing.duration_hours = item.get("duration_hours", existing.duration_hours)
            existing.synced_at = now
        else:
            session.add(HealthSleepSession(
                uid=uid,
                user_id=user_id,
                start_time=start_time,
                end_time=end_time,
                stage=item.get("stage", "unknown"),
                duration_hours=item.get("duration_hours", 0.0),
                synced_at=now,
            ))
        count += 1

    session.commit()
    logger.info(f"Health push sync: {count} records processed")
    return count


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
