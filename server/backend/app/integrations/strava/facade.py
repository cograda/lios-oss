"""strava's declared facade — capability `strava.query` (issue #195).

Added so the daily brief's Pulse section can merge Strava activities
alongside `health.query`'s workouts, instead of reporting on Apple Health
alone. `manifest.py` previously left `provides=[]` deliberately, per
writing-an-integration.md's rule that a facade isn't added speculatively —
this is that "once `system`'s briefing actually wants it" moment.

`activities()` distinguishes "not connected" from "connected, nothing in the
window" — `{"connected": False}` vs `{"connected": True, "activities": []}`.
That distinction is what lets `brief_render.render_pulse` say "Strava: not
connected" instead of silently reporting the same "none" a real empty week
would, which is the failure #195 was filed against for `health_workouts`
alone (a single always-empty-looking source read as "no workouts happened").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import json

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.integrations.strava.models import StravaActivity
from app.models.tokens import OAuthToken


class StravaFacade:
    def activities(self, session: Session, arguments: dict[str, Any]) -> str:
        """Recent activities for the caller, or `{"connected": False}`.

        `current_user_id()` rather than a passed-in id — this runs the same
        way every other daily-brief source does, through `brief.py`'s
        `_fetch_one`, which re-pins the ContextVar per worker thread before
        calling the facade method.
        """
        from app.auth.context import current_user_id

        user_id = current_user_id()

        token = (
            session.query(OAuthToken)
            .filter_by(provider="strava", user_id=user_id)
            .first()
        )
        if token is None:
            return json.dumps({"connected": False})

        days = int(arguments.get("days") or 7)
        since = datetime.now(timezone.utc) - timedelta(days=days)

        rows = (
            session.query(StravaActivity)
            .filter(
                StravaActivity.user_id == user_id,
                StravaActivity.start_date >= since,
            )
            .order_by(StravaActivity.start_date.desc())
            .all()
        )

        activities = [
            {
                "type": r.sport_type or r.activity_type or "activity",
                "name": r.name,
                "start": r.start_date.isoformat() if r.start_date else None,
                "duration_min": round(r.moving_time_s / 60.0, 1) if r.moving_time_s else 0.0,
                "distance_km": round(r.distance_m / 1000.0, 2) if r.distance_m else 0.0,
            }
            for r in rows
        ]

        return json.dumps({
            "connected": True,
            "days": days,
            "count": len(activities),
            "activities": activities,
        })

    def has_data(self, session: Session, user_id: int) -> bool:
        """Whether this user has ANY Strava activity stored, ever.

        Used by `/api/v1/instructions`' personalisation (sam-rollout D1)
        the same way every other facade's `has_data()` is. Not used to gate
        `strava_activities` in the daily brief on purpose — see
        `brief.py::build_sources`'s comment on that Source: gating on
        `has_data()` would omit the key entirely for a connected-but-quiet
        week, which is exactly the "source went silent" case the Pulse
        renderer needs to be able to name rather than silently drop.
        """
        count = (
            session.query(sa_func.count(StravaActivity.id))
            .filter(StravaActivity.user_id == user_id)
            .scalar()
        )
        return bool(count)


FACADE = StravaFacade()
