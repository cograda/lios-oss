"""Deadline watches — "it is past T and X still hasn't arrived".

Every axis in `system_alerts` asks an *elapsed-time* question: has this table
gone quiet for longer than N minutes. That shape cannot express the question
this module answers, because the thing being waited for has a wall-clock
deadline rather than a cadence:

> By 10am, last night's sleep should be in. If it isn't, say so — once.

A staleness threshold gets this wrong in both directions. Tight enough to
notice by 10am, and it fires every night while you are actually asleep and no
data is being written; loose enough not to, and it never notices at all. The
missing ingredient is the local time of day, which no `MAX(timestamp)` carries.

**Why these live in the notifications sweep rather than their own cron.** The
sweep already owns the two hard parts — the `notification_sends` ledger (so a
condition true from 10:00 until midnight notifies *once*, not every 15 minutes)
and the recovery ping (so a late-arriving export un-says the alert). A separate
cron would have to reinvent both. Expressing a deadline as an item that simply
*exists* while the condition holds gets the dedupe, the once-a-day resend
window and the recovery message for free.

**Why here and not in `apple_health`.** `apple_health` cannot depend on
`notify.push`: `notifications` depends on `system.alerts`, and `system` depends
on `health.query`, so the edge would close a cycle that `app/plugin/validate.py`
rejects at boot. `notifications` is the one package that can both read another
integration's data and push, so a check that needs both belongs here. Same
shape as `inbox` pulling WhatsApp notes rather than WhatsApp pushing them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

# Fallback when `sleep_deadline_timezone` is unset or unparseable. A deadline is
# meaningless in UTC for anyone who does not live there, and silently drifting an
# hour with DST would make "by 10am" mean 9am for half the year.
_DEFAULT_TZ = "Europe/Dublin"

# A deadline stops being useful once the day it refers to is over: at 23:00,
# "last night's sleep is missing" is no longer actionable, and leaving the item
# alive until midnight would push its recovery ping into the small hours. The
# window runs from the deadline hour to this hour, local time.
_WINDOW_END_HOUR = 22


@dataclass(frozen=True)
class SleepDeadline:
    """Resolved configuration for one person's sleep-by-deadline check."""

    user_id: int
    hour: int
    tz: ZoneInfo


def _resolve_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or _DEFAULT_TZ)
    except Exception:  # noqa: BLE001 — zoneinfo raises several unrelated types
        logger.warning("bad sleep_deadline_timezone %r; falling back to %s", name, _DEFAULT_TZ)
        return ZoneInfo(_DEFAULT_TZ)


def _configured() -> list[SleepDeadline]:
    """Parse the sleep-deadline config into one entry per watched user.

    Empty by default and empty on junk, so an unconfigured or mistyped key
    produces no checks rather than checks for the wrong person. Every skip is
    logged — a deadline watch that silently isn't running is worse than none,
    because you stop looking for the thing it was meant to tell you.
    """
    cfg = plugin_config("notifications")
    raw_ids = cfg.sleep_deadline_user_ids or []
    if not raw_ids:
        return []

    hour = cfg.sleep_deadline_hour
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        logger.warning("bad sleep_deadline_hour %r; deadline checks disabled", hour)
        return []

    tz = _resolve_tz(cfg.sleep_deadline_timezone)
    out: list[SleepDeadline] = []
    for raw in raw_ids:
        try:
            out.append(SleepDeadline(user_id=int(raw), hour=hour, tz=tz))
        except (TypeError, ValueError):
            logger.warning("ignoring non-numeric sleep_deadline_user_ids entry %r", raw)
    return out


def _night_due(now_local: datetime, hour: int) -> date | None:
    """Which night's sleep is overdue as of `now_local`, if any.

    Returns the night's date (the date the sleep *ended* on, matching
    `apple_health`'s bucketing) once the local clock is past `hour`, and None
    before the deadline or after the window closes.

    Before the deadline the answer is deliberately None rather than "not yet
    missing": the point of a deadline is that the export legitimately has until
    10am to arrive, so there is nothing to report at 08:00 even though the data
    is genuinely absent.
    """
    if now_local.hour < hour or now_local.hour >= _WINDOW_END_HOUR:
        return None
    return now_local.date()


def collect(session: Session, now: datetime | None = None) -> list:
    """Alert items for every deadline currently missed.

    Imported lazily inside the function body: `sweep` imports this module at
    module scope, so importing `AlertItem` from `sweep` at *our* module scope
    would be a circular import.
    """
    from app.integrations.notifications.sweep import AlertItem

    deadlines = _configured()
    if not deadlines:
        return []

    health = get_capability("health.query")
    items = []

    for d in deadlines:
        now_local = (now or datetime.now(d.tz)).astimezone(d.tz)
        night = _night_due(now_local, d.hour)
        if night is None:
            continue

        try:
            hours = health.slept_hours(session, user_id=d.user_id, night=night)
        except Exception:  # noqa: BLE001
            # One user's failed lookup must not lose the other's alert, and must
            # not invent a missing night out of a database error.
            logger.exception("sleep deadline lookup failed for user %s", d.user_id)
            continue

        if hours is not None:
            continue

        # Fingerprint carries the night, so tomorrow's miss is a fresh episode
        # rather than a resend of today's still-open row — without the date, a
        # run of missed mornings would notify once and then go quiet.
        items.append(
            AlertItem(
                fingerprint=f"deadline:sleep:{d.user_id}:{night.isoformat()}",
                title="lios: no sleep data for last night",
                body=(
                    f"Nothing recorded for the night of "
                    f"{(night - timedelta(days=1)).strftime('%a %d %b')} — still "
                    f"missing at {now_local.strftime('%H:%M')}. "
                    "Check Health Auto Export has run and Tailscale is up on the phone."
                ),
                severity="warning",
                target_user_id=d.user_id,
            )
        )

    return items
