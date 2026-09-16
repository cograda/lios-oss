"""Apple Health integration — push-based sync from Mac agent.

`PushSourceIntegration` conversion (V4 chunk 4.3, batch B). Data arrives
exclusively via the ingest route declared in the manifest (`routes.py`'s
`/health/push`, chunk 3.1) — there is nothing for the kernel to schedule, so
`sync()` is fully inherited from `PushSourceIntegration` (raises
`NotImplementedError` — the old hand-rolled `sync()` here just logged and
returned, since the manifest's `schedule=None` means the scheduler never
called it and no test exercised the manual-trigger path for this
integration; see chunk file "Batch progress" for the full note on this
judgment call). No `probe()` — no separate liveness check is declared for
this integration in the manifest's `background_tasks`.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import get_db
from app.integrations.apple_health.models import (
    HealthDailyMetric,
    HealthSleepSession,
    HealthWorkout,
)
from app.integrations.apple_health.tools import get_mcp_tools
from app.plugin.bases import PushSourceIntegration

logger = logging.getLogger(__name__)


class AppleHealthIntegration(PushSourceIntegration):
    @property
    def name(self) -> str:
        return "apple_health"

    @property
    def display_name(self) -> str:
        return "Apple Health"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return health summary for the dashboard."""
        db = get_db()
        today = date.today()
        yesterday = today - timedelta(days=1)

        with db.session() as session:
            # Today's metrics
            today_rows = (
                session.query(HealthDailyMetric)
                .filter(HealthDailyMetric.date == today)
                .all()
            )
            metrics = {r.metric_type: round(r.value, 1) for r in today_rows}

            # Last night's sleep
            start_of_today = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
            prev_evening = start_of_today - timedelta(hours=12)
            sleep_rows = (
                session.query(HealthSleepSession)
                .filter(
                    HealthSleepSession.end_time >= prev_evening,
                    HealthSleepSession.start_time < start_of_today + timedelta(days=1),
                )
                .all()
            )
            sleep_total = round(sum(r.duration_hours for r in sleep_rows), 1)

            # Workouts this week
            week_start = today - timedelta(days=today.weekday())
            workout_count = (
                session.query(HealthWorkout)
                .filter(HealthWorkout.start_time >= datetime(
                    week_start.year, week_start.month, week_start.day, tzinfo=timezone.utc
                ))
                .count()
            )

            return {
                "today": metrics,
                "sleep_hours": sleep_total,
                "workouts_this_week": workout_count,
            }

    # is_configured(): default — True iff health_push_token is set (required
    # in this integration's config_schema).
