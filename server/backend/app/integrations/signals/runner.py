"""`signals_watchers_tick` — the cron job that drives every registered watcher.

Every 5 minutes: for each watcher, work out whether a scheduled window is
currently open. If so, open the run (baseline grab) if it doesn't exist yet,
poll it if a poll is due, or close it if the window has just ended. Inlet
events call `Watcher.check()` directly (via `routes.py`'s background task,
with the settle delay) — this tick is the fallback cadence plus open/close,
not the primary detection path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db import get_db
from app.integrations.signals.models import WatchRun
from app.integrations.signals.watchers.registry import WATCHERS
from app.integrations.signals.watchers.window import DUBLIN, is_closed, window_for_night

logger = logging.getLogger(__name__)

TICK_MINUTES = 5


async def run_tick() -> None:
    now = datetime.now(timezone.utc)
    db = get_db()

    for watcher in WATCHERS:
        window = watcher.current_window(now)
        with db.session() as session:
            if window is not None:
                run = watcher.get_or_open_run(session, window)
                if run.status == "watching":
                    from app.integrations.signals.watchers.window import should_poll

                    if should_poll(run.opened_at, now, watcher.poll_minutes, TICK_MINUTES):
                        try:
                            await watcher.check(session, run, wait_settle=False)
                        except Exception:  # noqa: BLE001
                            logger.exception("[signals] %s: poll check failed", watcher.name)
            else:
                # Not currently inside a window — close out any run whose
                # window has just ended and is still "watching". Only the
                # most recent open run needs checking; older ones were
                # already closed on a prior tick.
                run = (
                    session.query(WatchRun)
                    .filter_by(watcher=watcher.name, status="watching")
                    .order_by(WatchRun.opened_at.desc())
                    .first()
                )
                if run is not None:
                    night = _date_from_iso(run.night_date)
                    win = window_for_night(night, watcher.start, watcher.end)
                    if is_closed(win, now):
                        try:
                            watcher.close_run(session, run)
                        except Exception:  # noqa: BLE001
                            logger.exception("[signals] %s: close failed", watcher.name)


def _date_from_iso(value: str):
    from datetime import date

    return date.fromisoformat(value)
