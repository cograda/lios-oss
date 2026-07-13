"""Data freshness probes — detect when an integration's *output* has stalled.

Distinct from `SyncState`, which only records whether the sync *job* ran.
A job can succeed (or be a no-op like the WhatsApp embedding chunker) while
the underlying data has stopped flowing. These probes query the actual data
tables and surface "no new records in N hours" as a separate alert axis.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session


# Per-integration thresholds in seconds. Tuned for "this much silence is
# suspicious for the user's normal pattern", not for "the upstream might
# have hiccuped". Bias toward few false positives.
DATA_FRESHNESS_THRESHOLDS: dict[str, int] = {
    "whatsapp": 24 * 3600,
    "google_mail": 6 * 3600,
    "lastfm": 48 * 3600,
    "apple_health": 36 * 3600,
    # Reminders bridge — the comar-client daemon pings /reminders/verified
    # every ~30s when healthy. 5 min is the smallest threshold that won't
    # false-positive on a brief network blip. NB: probe is MAX across
    # users (matches existing pattern); a healthy Sam daemon will mask
    # a dead Alex daemon. Acceptable until we go per-user.
    "apple_reminders": 5 * 60,
}


@dataclass
class FreshnessResult:
    integration: str
    latest_ts: datetime | None
    threshold_seconds: int
    age_seconds: int | None  # None if no rows ever


def _probe(session: Session, integration: str) -> datetime | None:
    """Return MAX(timestamp) for the integration's primary table, or None."""
    if integration == "whatsapp":
        from app.integrations.whatsapp.models import WhatsAppMessage
        return session.query(func.max(WhatsAppMessage.timestamp)).scalar()
    if integration == "google_mail":
        from app.integrations.google_mail.models import MailMessage
        return session.query(func.max(MailMessage.date)).scalar()
    if integration == "lastfm":
        from app.integrations.lastfm.models import Scrobble
        return session.query(func.max(Scrobble.played_at)).scalar()
    if integration == "apple_health":
        # Daily metrics + sleep flow continuously; workouts are sporadic
        # by nature. Probe synced_at — "when did a record last land?" —
        # rather than the `date` column, which is the calendar day the
        # metric describes (always ≤ today, would give a misleading
        # negative age if you used end-of-day).
        from app.integrations.apple_health.models import HealthDailyMetric
        return session.query(func.max(HealthDailyMetric.synced_at)).scalar()
    if integration == "apple_reminders":
        # Bridge liveness — when did any daemon last ping /reminders/verified.
        # Deliberately NOT max(Reminder.synced_at) — that's data-change time
        # and stays put during quiet periods, producing false alarms.
        from app.models.users import User
        return session.query(func.max(User.reminders_verified_at)).scalar()
    return None


def check_all(session: Session) -> list[FreshnessResult]:
    """Run every freshness probe. Always returns a result per integration —
    the caller decides whether to alert based on `age_seconds > threshold_seconds`.
    """
    now = datetime.now(timezone.utc)
    results: list[FreshnessResult] = []
    for name, threshold in DATA_FRESHNESS_THRESHOLDS.items():
        latest = _probe(session, name)
        age = None
        if latest is not None:
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            age = int((now - latest).total_seconds())
        results.append(
            FreshnessResult(
                integration=name,
                latest_ts=latest,
                threshold_seconds=threshold,
                age_seconds=age,
            )
        )
    return results


def format_age(seconds: int) -> str:
    """Compact human age string for alert messages."""
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d {h}h" if h else f"{d}d"
