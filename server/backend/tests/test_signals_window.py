"""Watcher window maths (unit tier, no DB) — weekday + cross-midnight +
BST/GMT boundary, and the poll-cadence gate.

Mutation check (per the brief): flipping `<=` to `<` in `window_for_night`'s
midnight-crossing test, or the off-by-one in `active_window`'s day-before
check, breaks `test_window_crosses_midnight_into_next_weekday` and
`test_active_window_checks_yesterday_too` respectively — verified by hand
during development (reinstate either bug locally and one of these two goes
red).
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.integrations.signals.watchers.window import (
    DUBLIN, active_window, is_closed, should_poll, window_for_night,
)

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def test_window_for_night_same_day_when_end_after_start():
    w = window_for_night(date(2026, 9, 8), time(9, 0), time(17, 0))
    assert w.open_dt.date() == date(2026, 9, 8)
    assert w.close_dt.date() == date(2026, 9, 8)


def test_window_crosses_midnight_into_next_weekday():
    w = window_for_night(date(2026, 9, 10), time(20, 30), time(0, 30))
    assert w.open_dt == datetime(2026, 9, 10, 20, 30, tzinfo=DUBLIN)
    assert w.close_dt == datetime(2026, 9, 11, 0, 30, tzinfo=DUBLIN)


def test_active_window_matches_within_bounds():
    # Thursday 2026-09-10, 21:00 — inside the milk window (Thu=THU, opens 20:30)
    now = datetime(2026, 9, 10, 21, 0, tzinfo=DUBLIN)
    w = active_window(now, frozenset({THU}), time(20, 30), time(0, 30))
    assert w is not None
    assert w.night_date == date(2026, 9, 10)


def test_active_window_checks_yesterday_too():
    # 00:15 on Friday morning — still inside Thursday night's cross-midnight window.
    now = datetime(2026, 9, 11, 0, 15, tzinfo=DUBLIN)
    w = active_window(now, frozenset({THU}), time(20, 30), time(0, 30))
    assert w is not None
    assert w.night_date == date(2026, 9, 10)


def test_active_window_none_outside_bounds():
    now = datetime(2026, 9, 11, 1, 0, tzinfo=DUBLIN)  # past 00:30 close
    w = active_window(now, frozenset({THU}), time(20, 30), time(0, 30))
    assert w is None


def test_active_window_none_on_wrong_weekday():
    # Monday — not one of Sun/Tue/Thu.
    now = datetime(2026, 9, 7, 21, 0, tzinfo=DUBLIN)
    w = active_window(now, frozenset({SUN, TUE, THU}), time(20, 30), time(0, 30))
    assert w is None


def test_is_closed():
    w = window_for_night(date(2026, 9, 10), time(20, 30), time(0, 30))
    assert not is_closed(w, datetime(2026, 9, 10, 22, 0, tzinfo=DUBLIN))
    assert is_closed(w, datetime(2026, 9, 11, 0, 30, tzinfo=DUBLIN))
    assert is_closed(w, datetime(2026, 9, 11, 1, 0, tzinfo=DUBLIN))


def test_active_window_either_side_of_bst_to_gmt_boundary():
    # Ireland's clocks went back 2026-10-25 (BST -> GMT), at 02:00 local — so
    # an evening window (20:30-00:30) sampled the night BEFORE the transition
    # is still BST throughout, and the night ON (or after) the transition is
    # already GMT throughout. What matters is that `active_window` resolves
    # the right night in both regimes despite the UTC offset underneath
    # wall-clock 20:30/00:30 having changed by one hour between them.
    before = date(2026, 10, 24)  # Saturday, still BST all evening
    w_before = window_for_night(before, time(20, 30), time(0, 30))
    assert w_before.open_dt.utcoffset().total_seconds() == 3600
    assert w_before.close_dt.utcoffset().total_seconds() == 3600

    on_transition = date(2026, 10, 25)  # Sunday, GMT from 02:00 onward
    w_on = window_for_night(on_transition, time(20, 30), time(0, 30))
    assert w_on.open_dt.utcoffset().total_seconds() == 0
    assert w_on.close_dt.utcoffset().total_seconds() == 0

    # A `now` given in UTC still resolves to the correct night on both sides.
    now_utc_before = datetime(2026, 10, 24, 21, 0, tzinfo=ZoneInfo("UTC"))  # 22:00 BST
    found_before = active_window(now_utc_before, frozenset({before.weekday()}), time(20, 30), time(0, 30))
    assert found_before is not None and found_before.night_date == before

    now_utc_on = datetime(2026, 10, 25, 22, 0, tzinfo=ZoneInfo("UTC"))  # 22:00 GMT
    found_on = active_window(now_utc_on, frozenset({on_transition.weekday()}), time(20, 30), time(0, 30))
    assert found_on is not None and found_on.night_date == on_transition


def test_should_poll_gates_on_bucket_rollover():
    opened = datetime(2026, 9, 10, 20, 30, tzinfo=DUBLIN)
    # 5-minute ticks, poll every 15 minutes -> due at +15, +30, +45, not +5/+10.
    assert not should_poll(opened, opened, poll_minutes=15, tick_minutes=5)
    assert not should_poll(opened, opened.replace(minute=35), poll_minutes=15, tick_minutes=5)
    assert not should_poll(opened, opened.replace(minute=40), poll_minutes=15, tick_minutes=5)
    assert should_poll(opened, opened.replace(minute=45), poll_minutes=15, tick_minutes=5)


def test_should_poll_zero_means_every_tick():
    opened = datetime(2026, 9, 10, 20, 30, tzinfo=DUBLIN)
    assert should_poll(opened, opened.replace(minute=35), poll_minutes=0, tick_minutes=5)
