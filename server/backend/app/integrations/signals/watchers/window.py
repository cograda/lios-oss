"""Pure window-maths — no DB, no IO, so this is unit-testable on its own.

A watcher's "window" is defined by weekdays + a local start/end time-of-day
in Europe/Dublin, and may cross midnight (e.g. 20:30 -> 00:30 the next
day). The window is named after the day it OPENS on ("Tuesday's milk"), even
though a cross-midnight window closes on Wednesday morning.

`zoneinfo.ZoneInfo("Europe/Dublin")` on an aware datetime is what makes the
BST/GMT boundary correct for free — Ireland's clocks changing does not shift
wall-clock 20:30, but it does shift the UTC instant that wall-clock time
maps to, which is exactly what `astimezone` handles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DUBLIN = ZoneInfo("Europe/Dublin")


@dataclass(frozen=True)
class Window:
    night_date: date  # the date the window OPENED on
    open_dt: datetime  # aware, Europe/Dublin
    close_dt: datetime  # aware, Europe/Dublin


def window_for_night(night_date: date, start: time, end: time, tz: ZoneInfo = DUBLIN) -> Window:
    """The window that opens on `night_date`. Crosses midnight iff `end <= start`."""
    open_dt = datetime.combine(night_date, start, tzinfo=tz)
    end_date = night_date + timedelta(days=1) if end <= start else night_date
    close_dt = datetime.combine(end_date, end, tzinfo=tz)
    return Window(night_date, open_dt, close_dt)


def active_window(
    now: datetime, weekdays: frozenset[int], start: time, end: time, tz: ZoneInfo = DUBLIN,
) -> Window | None:
    """The window (if any) that `now` currently falls inside, restricted to
    windows whose OPENING day's weekday is in `weekdays` (Monday=0 .. Sunday=6,
    matching `date.weekday()`).

    Checks both "today's window" and "yesterday's window" (which may still be
    open past midnight) — a cross-midnight window opened yesterday can still
    contain `now` in the small hours.
    """
    now = now.astimezone(tz)
    for candidate_night in (now.date(), now.date() - timedelta(days=1)):
        if candidate_night.weekday() not in weekdays:
            continue
        window = window_for_night(candidate_night, start, end, tz)
        if window.open_dt <= now < window.close_dt:
            return window
    return None


def is_closed(window: Window, now: datetime, tz: ZoneInfo = DUBLIN) -> bool:
    return now.astimezone(tz) >= window.close_dt


def should_poll(opened_at: datetime, now: datetime, poll_minutes: int, tick_minutes: int = 5) -> bool:
    """Whether a poll is due, given the window opened at `opened_at` and this
    tick is running at `now`. Ticks arrive every `tick_minutes`; a poll is due
    on the tick whose elapsed-minutes bucket rolls over `poll_minutes` — e.g.
    with a 5-minute tick and poll_minutes=15, ticks at +15, +30, +45... poll.

    Elapsed is clamped to 0 so a clock that ticks *before* `opened_at` (the
    same run, opened moments ago) never divides by a negative number.
    """
    elapsed_min = max(0, (now - opened_at).total_seconds() / 60)
    if poll_minutes <= 0:
        return True
    bucket = int(elapsed_min // tick_minutes)
    poll_every = max(1, round(poll_minutes / tick_minutes))
    return bucket > 0 and bucket % poll_every == 0
