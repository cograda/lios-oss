"""MCP tools over stored Strava activities.

All unit conversion happens here, on read. `models.StravaActivity` stores
exactly what Strava returned — metres, seconds, metres per second — so a
conversion that turns out to be wrong is a change to this file, not an
unrecoverable corruption of the archive.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.strava.models import StravaActivity
from app.models.tokens import OAuthToken
from app.plugin.sync_runtime import SyncCursor
from app.tools import CustomTool, ExtraFilter, ListTool, StatsTool, ToolAnnotations

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

#: Metres per second to kilometres per hour.
_MS_TO_KMH = 3.6


def _pace_per_km(distance_m: float, moving_time_s: int) -> str | None:
    """Minutes:seconds per kilometre, or None when it would be meaningless.

    A zero distance (a gym session, a swim logged without GPS) has no pace;
    returning `0:00` would read as an impossibly fast one rather than as the
    absence of a figure.
    """
    if not distance_m or not moving_time_s:
        return None
    seconds_per_km = moving_time_s / (distance_m / 1000.0)
    minutes, seconds = divmod(int(round(seconds_per_km)), 60)
    return f"{minutes}:{seconds:02d}"


def _activity_to_dict(row: StravaActivity) -> dict[str, Any]:
    """Present one activity in human units."""
    local_start = None
    if row.start_date is not None and row.utc_offset_seconds is not None:
        local_start = (
            row.start_date + timedelta(seconds=row.utc_offset_seconds)
        ).strftime("%Y-%m-%d %H:%M")

    result: dict[str, Any] = {
        "id": row.strava_id,
        "name": row.name,
        "sport": row.sport_type or row.activity_type,
        "date": row.start_date.strftime("%Y-%m-%d") if row.start_date else None,
        "local_start": local_start,
        "distance_km": round(row.distance_m / 1000.0, 2),
        "moving_time_min": round(row.moving_time_s / 60.0, 1),
        "elapsed_time_min": round(row.elapsed_time_s / 60.0, 1),
        "elevation_gain_m": round(row.total_elevation_gain_m, 1),
        "pace_per_km": _pace_per_km(row.distance_m, row.moving_time_s),
    }
    if row.average_speed_ms:
        result["avg_speed_kmh"] = round(row.average_speed_ms * _MS_TO_KMH, 1)
    if row.average_heartrate:
        result["avg_hr_bpm"] = round(row.average_heartrate)
    if row.max_heartrate:
        result["max_hr_bpm"] = round(row.max_heartrate)
    if row.average_watts:
        result["avg_watts"] = round(row.average_watts)
        # Whether these watts were measured or estimated changes what they
        # mean, so the flag travels with the number rather than being
        # discoverable only by re-reading the table.
        result["watts_measured"] = bool(row.device_watts)
    if row.suffer_score:
        result["relative_effort"] = round(row.suffer_score)
    for flag in ("trainer", "commute", "manual", "private"):
        if getattr(row, flag):
            result[flag] = True
    return result


def compute_stats(session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
    """Totals and per-sport breakdown over a period (default: all time)."""
    from app.tools.helpers import scoped_query

    days = arguments.get("days")
    query = scoped_query(session, StravaActivity)
    if days:
        since = datetime.now(timezone.utc) - timedelta(days=int(days))
        query = query.filter(StravaActivity.start_date >= since)
    if sport := arguments.get("sport"):
        query = query.filter(StravaActivity.sport_type == sport)

    rows = query.all()
    if not rows:
        return {"count": 0, "period_days": days, "message": "No activities stored."}

    by_sport: dict[str, dict[str, float]] = {}
    for row in rows:
        key = row.sport_type or row.activity_type or "Unknown"
        bucket = by_sport.setdefault(
            key, {"count": 0, "distance_km": 0.0, "moving_hours": 0.0, "elevation_m": 0.0}
        )
        bucket["count"] += 1
        bucket["distance_km"] += row.distance_m / 1000.0
        bucket["moving_hours"] += row.moving_time_s / 3600.0
        bucket["elevation_m"] += row.total_elevation_gain_m

    for bucket in by_sport.values():
        bucket["distance_km"] = round(bucket["distance_km"], 1)
        bucket["moving_hours"] = round(bucket["moving_hours"], 1)
        bucket["elevation_m"] = round(bucket["elevation_m"])

    first = min(r.start_date for r in rows)
    last = max(r.start_date for r in rows)

    return {
        "count": len(rows),
        "period_days": days,
        "first_activity": first.strftime("%Y-%m-%d"),
        "last_activity": last.strftime("%Y-%m-%d"),
        "total_distance_km": round(sum(r.distance_m for r in rows) / 1000.0, 1),
        "total_moving_hours": round(sum(r.moving_time_s for r in rows) / 3600.0, 1),
        "total_elevation_m": round(sum(r.total_elevation_gain_m for r in rows)),
        "by_sport": dict(
            sorted(by_sport.items(), key=lambda kv: kv[1]["count"], reverse=True)
        ),
    }


def handle_strava_backfill(session: Session, arguments: dict) -> str:
    """Walk the caller's full Strava history into Postgres."""
    from app.integrations.strava.sync import backfill_activities

    user_id = current_user_id()
    result = backfill_activities(
        session,
        user_id=user_id,
        resume=bool(arguments.get("resume", True)),
        max_pages=int(arguments.get("max_pages", 100)),
    )
    # `status` is echoed verbatim rather than flattened to ok/failed: the
    # caller needs to distinguish "complete" from "rate_limited, re-run to
    # continue", and a boolean cannot carry that.
    return json.dumps(result, indent=2, default=str)


def handle_strava_status(session: Session, arguments: dict) -> str:
    """Connection state, stored coverage, and backfill progress."""
    user_id = current_user_id()

    token = (
        session.query(OAuthToken)
        .filter_by(provider="strava", user_id=user_id)
        .first()
    )

    count, first, last = (
        session.query(
            sa_func.count(StravaActivity.id),
            sa_func.min(StravaActivity.start_date),
            sa_func.max(StravaActivity.start_date),
        )
        .filter(StravaActivity.user_id == user_id)
        .one()
    )

    return json.dumps(
        {
            "connected": token is not None,
            "athlete_id": token.account_email if token else None,
            "scopes": token.scopes if token else None,
            "needs_reauth": bool(token and token.needs_reauth_at),
            "needs_reauth_reason": token.needs_reauth_reason if token else None,
            "token_expires_at": (
                token.expires_at.isoformat() if token and token.expires_at else None
            ),
            "activities_stored": count,
            "earliest": first.strftime("%Y-%m-%d") if first else None,
            "latest": last.strftime("%Y-%m-%d") if last else None,
            "backfill_status": SyncCursor.get(
                session, "strava", "backfill_status", user_id=user_id
            ),
            "connect_url": None if token else "/api/strava/connect?user=<your-name>",
        },
        indent=2,
    )


def get_mcp_tools() -> list[dict[str, Any]]:
    return [
        ListTool(
            name="strava_activities",
            description=(
                "List Strava activities — runs, rides, walks, swims and gym "
                "sessions — with distance, moving time, pace, elevation, heart "
                "rate and power. Filter by sport type and date range."
            ),
            model=StravaActivity,
            timestamp_col="start_date",
            to_dict=_activity_to_dict,
            default_limit=20,
            max_limit=200,
            extra_filters=[
                ExtraFilter(
                    param_name="sport",
                    column="sport_type",
                    description=(
                        "Strava sport type, e.g. Run, Ride, GravelRide, Walk, "
                        "Swim, WeightTraining, Hike."
                    ),
                ),
                ExtraFilter(
                    param_name="name_contains",
                    column="name",
                    description="Substring of the activity title.",
                    match_mode="ilike",
                ),
            ],
            category="fitness",
            examples=[
                "Show my recent Strava runs",
                "What rides did I do in July?",
                "List my longest activities this year",
            ],
        ).build(),
        StatsTool(
            name="strava_stats",
            description=(
                "Aggregate Strava totals — activity count, distance, moving "
                "time and elevation — broken down by sport, over a period or "
                "all time."
            ),
            model=StravaActivity,
            compute=compute_stats,
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Look back this many days. Omit for all time.",
                    },
                    "sport": {
                        "type": "string",
                        "description": "Restrict to one sport type, e.g. Run or Ride.",
                    },
                },
            },
            category="fitness",
            examples=[
                "How far have I run this year?",
                "Strava totals for the last 90 days",
            ],
        ).build(),
        CustomTool(
            name="strava_status",
            description=(
                "Whether Strava is connected for you, what date range of "
                "activities is stored, and whether a historical backfill is "
                "complete or was interrupted."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_strava_status,
            annotations=_READ_ONLY,
            category="fitness",
            examples=["Is Strava connected?", "How much Strava history do we have?"],
        ).build(),
        CustomTool(
            name="strava_backfill",
            description=(
                "Import your full Strava history into comar. Resumable — if it "
                "stops on a rate limit, run it again to continue from where it "
                "left off. Safe to re-run; existing activities are updated, not "
                "duplicated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "resume": {
                        "type": "boolean",
                        "description": (
                            "Continue from the saved cursor (default true). "
                            "False restarts from the most recent activity."
                        ),
                        "default": True,
                    },
                    "max_pages": {
                        "type": "integer",
                        "description": (
                            "Stop after this many API pages of 200 activities "
                            "(default 100). Bounds one invocation against the "
                            "Strava rate limit."
                        ),
                        "default": 100,
                    },
                },
            },
            handler=handle_strava_backfill,
            # Writes rows, but re-running converges on the same state rather
            # than accumulating — idempotent, not destructive.
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
            category="fitness",
            examples=[
                "Import all my Strava history",
                "Resume the Strava backfill",
            ],
        ).build(),
    ]
