"""apple_health's declared facade — capability `health.query` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.apple_health`. Consumers: `system`'s morning-briefing
composite (`summary`) and `system`'s alerts axis (`coverage_gaps`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.integrations.apple_health.models import HealthDailyMetric, HealthSleepSession
from app.models.tokens import SyncState
from app.integrations.apple_health.tools import (
    handle_health_sleep,
    handle_health_summary,
    handle_health_trends,
    handle_health_workouts,
    sleep_window,
)

# How far back to look for holes. Two weeks is long enough to cover a holiday
# and short enough that the alert stays actionable — Health Auto Export's
# rolling-window re-send should have repaired anything inside it.
DEFAULT_COVERAGE_DAYS = 14

# How long the phone may go without *attempting* a push before we say so.
#
# ⚠️ Provisional, and knowingly so. This repo has twice thresholded a source
# without measuring its cadence first (lastfm, 13 and 19 August), so: nothing
# recorded push *attempts* until `routes._record_push` existed, which means
# there is no attempt-cadence history to derive this from yet. 12h is chosen to
# sit clearly inside the 36h data-staleness threshold while staying outside the
# 12-20h *data* gaps the manifest documents as normal.
#
# `SyncHistory` now accumulates one row per attempt with trigger="push", so the
# real distribution is measurable within a day or two. Re-measure and tune this
# before treating a firing as evidence of anything.
DEFAULT_PUSH_SILENCE_HOURS = 12


class AppleHealthFacade:
    def summary(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_health_summary(session, arguments)

    def sleep(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_health_sleep(session, arguments)

    def trends(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_health_trends(session, arguments)

    def workouts(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_health_workouts(session, arguments)

    def slept_hours(self, session: Session, *, user_id: int, night: date) -> float | None:
        """Total recorded sleep hours for `user_id` on the night ending `night`.

        `None` means *no session rows at all* for that night — the thing a
        deadline check cares about. `0.0` would be a night that was recorded as
        entirely awake, which is data, not absence, so the two must not collapse.

        Explicitly `user_id`-argumented rather than going through
        `scoped_query`: the caller is a cron with no bound user context, and
        the whole point is to ask about a specific person.

        Shares `tools.sleep_window` with the `health_sleep` tool so both agree
        on which night a session belongs to.
        """
        start, end = sleep_window(night)
        rows = (
            session.query(HealthSleepSession.duration_hours)
            .filter(
                HealthSleepSession.user_id == user_id,
                HealthSleepSession.end_time >= start,
                HealthSleepSession.end_time < end,
            )
            .all()
        )
        if not rows:
            return None
        return round(sum(r[0] or 0.0 for r in rows), 2)

    def has_data(self, session: Session, user_id: int) -> bool:
        """Cheap presence check — used by `app.mcp.instructions` to decide
        whether to offer this integration in a user's personalized render
        (sam-rollout D1)."""
        count = (
            session.query(func.count(HealthDailyMetric.id))
            .filter(HealthDailyMetric.user_id == user_id)
            .scalar()
        )
        return bool(count)

    def push_silence(
        self,
        session: Session,
        *,
        hours: int = DEFAULT_PUSH_SILENCE_HOURS,
    ) -> dict[str, Any] | None:
        """How long since the source last *attempted* a push, if that is too long.

        A third question, distinct from both of its neighbours:

        - the manifest staleness probe asks "how old is the newest row"
        - `coverage_gaps` asks "is any day missing"
        - this asks "is the phone still calling us at all"

        The first two are answered from data comar received, so both are blind
        for as long as it takes their thresholds to expire - and the data probe
        is at 36h precisely because real export gaps run 12-20h. Measured
        2026-08-23: the last push landed 09:00 on 08-22 and comar still reported
        `apple_health: status "ok"` 37 hours later. Nothing was wrong with the
        pipeline; the phone had simply stopped calling, most likely Tailscale
        off or iOS suspending Health Auto Export's background schedule.

        Reads `SyncState.last_sync_at`, which for this push_source means "last
        attempt" rather than "last success" - `_update_sync_state` stamps it on
        every outcome. That is what makes the signal sharp: it separates "the
        phone is not calling" from "the phone is calling and being rejected",
        which axis 1 already reports via `consecutive_failures`.

        Returns None when healthy, or `{"hours_silent", "threshold_hours",
        "last_attempt", "last_status"}`.
        """
        state = (
            session.query(SyncState).filter_by(integration="apple_health").first()
        )
        # No row at all means nothing has ever pushed. That is an onboarding
        # state, not an outage, and `has_data()` is what decides whether this
        # integration should be offered to a user in the first place.
        if state is None or state.last_sync_at is None:
            return None

        last = state.last_sync_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        silent_for = datetime.now(timezone.utc) - last
        if silent_for < timedelta(hours=hours):
            return None

        return {
            "hours_silent": round(silent_for.total_seconds() / 3600, 1),
            "threshold_hours": hours,
            "last_attempt": last.isoformat(),
            "last_status": state.last_sync_status,
        }

    def coverage_gaps(
        self,
        session: Session,
        *,
        days: int = DEFAULT_COVERAGE_DAYS,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Days in the trailing window with no health data at all, per user.

        This is a *different* question from the manifest's staleness probe, and
        the difference is the whole reason it exists. That probe asks
        `MAX(synced_at)` — "did a push arrive recently". With Health Auto Export
        re-sending a trailing window, a push that carries only old rows still
        bumps `synced_at`, so the probe stays green while the `date` axis has a
        hole in it. A hole is exactly what an off-network stretch leaves behind.

        Scoped per user on purpose. The kernel's freshness probe takes
        `MAX(synced_at)` across the whole table, so one household member's
        working phone masks another's dead one — an easy thing to not notice
        for months.

        Today is excluded: Health Auto Export runs on a schedule and the
        current day is legitimately incomplete until it does.

        Returns one entry per user who has *ever* pushed health data (a user
        with no data at all isn't "missing days", they're not set up), each
        `{"user_id", "missing_days": [ISO dates], "checked_days"}`.
        Users with full coverage are omitted, so an empty list means all good.
        """
        days = max(1, min(days, 365))
        today = date.today()
        window_end = today - timedelta(days=1)
        window_start = window_end - timedelta(days=days - 1)
        expected = {window_start + timedelta(days=i) for i in range(days)}

        q = session.query(
            HealthDailyMetric.user_id, HealthDailyMetric.date
        ).filter(HealthDailyMetric.date.between(window_start, window_end))
        if user_id is not None:
            q = q.filter(HealthDailyMetric.user_id == user_id)

        present: dict[int, set[date]] = {}
        for uid, d in q.distinct().all():
            present.setdefault(uid, set()).add(d)

        # Users with data in the window but not necessarily every day. A user
        # whose data predates the window entirely still counts as "set up", so
        # seed from the full set of user_ids that have ever pushed.
        seen_q = session.query(HealthDailyMetric.user_id).distinct()
        if user_id is not None:
            seen_q = seen_q.filter(HealthDailyMetric.user_id == user_id)
        known_users = [row[0] for row in seen_q.all()]

        out: list[dict[str, Any]] = []
        for uid in sorted(known_users):
            missing = sorted(expected - present.get(uid, set()))
            if missing:
                out.append({
                    "user_id": uid,
                    "missing_days": [d.isoformat() for d in missing],
                    "checked_days": days,
                })
        return out


FACADE = AppleHealthFacade()
