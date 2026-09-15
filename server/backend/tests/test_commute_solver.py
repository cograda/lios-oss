"""Solver fixtures — the reason the solver is a standalone module and not a
knot of template sensors. Each test hands `solve()` a hand-built world and
asserts the instruction. Times are wall-clock on one fixed date; the solver
is tz-naive local.

`solve()` is direction-neutral — buses/darts arrive pre-filtered to one leg
of one Route by client.py, so these fixtures use direction-neutral
`depart`/`arrive` field names throughout and never reference Route at all.
"""

from datetime import datetime, time

from app.integrations.commute.domain import (
    ArriveBy, BusDeparture, DartService, DEFAULT, DepartAfter, mask_for_logging_only,
)
from app.integrations.commute.solver import solve, solve_dart_only, solve_return

DAY = datetime(2026, 7, 3)  # a Friday; date only matters for combining with times


def at(h, m):
    return datetime.combine(DAY.date(), time(h, m))


def bus(dep_h, dep_m, arr_h, arr_m, route="L2", conf="live", delay=0.0):
    return BusDeparture(
        trip_id=f"{route}_{dep_h}{dep_m}",
        route=route,
        depart=at(dep_h, dep_m),
        arrive=at(arr_h, arr_m),
        depart_confidence=conf,
        arrive_confidence=conf,
        delay_min=delay,
    )


def dart(dep_h, dep_m, arr_h, arr_m, code=None):
    return DartService(
        traincode=code or f"E{dep_h}{dep_m}",
        depart=at(dep_h, dep_m),
        arrive=at(arr_h, arr_m),
        destination="Howth",
    )


CFG = DEFAULT
ARRIVE_857 = ArriveBy(deadline=time(8, 57))

# Two DARTs ~15 min apart: T1 makes the 08:57 deadline, T2 arrives late.
T1 = dart(8, 35, 8, 57, "T1")   # the deadline-making train
T2 = dart(8, 50, 9, 12, "T2")   # next one, arrives after 08:57
DARTS = [T1, T2]


# ---------------------------------------------------------------------------
# ArriveBy objective
# ---------------------------------------------------------------------------

def test_on_time_is_comfortable():
    """Bus arrives Howth with plenty of buffer -> aim T1, comfortable."""
    now = at(8, 0)
    # arrive 08:20 -> ready 08:26 -> catches T1 (dep 08:35), slack 9 min
    d = solve([bus(8, 5, 8, 20)], DARTS, now, ARRIVE_857, CFG)
    assert d.state == "comfortable"
    assert d.target_train.traincode == "T1"
    assert d.leave_in_min == 5
    assert not d.degraded


def test_thin_margin_is_tight():
    """Bus arrives Howth just inside the buffer -> tight, not comfortable."""
    now = at(8, 0)
    # arrive 08:27 -> ready 08:33 -> catches T1 (08:35), slack 2 min -> tight
    d = solve([bus(8, 10, 8, 27)], DARTS, now, ARRIVE_857, CFG)
    assert d.state == "tight"
    assert d.target_train.traincode == "T1"


def test_late_bus_downgrades_to_next_train():
    """A slipped bus that misses T1 must surface the next train and how late
    it makes you, not pretend or hedge."""
    now = at(8, 20)
    # arrive 08:40 -> ready 08:46 -> can't board T1 (08:35), catches T2 (08:50)
    # -> arrive 09:12, ~12 min late
    d = solve([bus(8, 25, 8, 40, delay=8.0)], DARTS, now, ARRIVE_857, CFG)
    assert d.state == "next_train"
    assert d.target_train.traincode == "T2"
    assert "late" in d.status_text.lower()


def test_pick_latest_bus_that_still_makes_deadline():
    """Given several on-time buses, recommend the latest-departing (leave as
    late as possible)."""
    now = at(8, 0)
    buses = [
        bus(8, 5, 8, 18, route="L1"),   # early, comfortable
        bus(8, 12, 8, 25, route="L2"),  # later, arrive 08:25 ready 08:31 still makes T1
    ]
    d = solve(buses, DARTS, now, ARRIVE_857, CFG)
    assert d.state in ("comfortable", "tight")
    assert d.target_bus.depart == at(8, 12)   # the later of the two
    assert d.leave_in_min == 12


def test_nothing_catchable_is_missed():
    """No future bus reaches any DART -> honest 'missed', no fabricated leave-now."""
    now = at(8, 55)  # both DARTs effectively gone / unreachable
    d = solve([bus(9, 0, 9, 20)], DARTS, now, ARRIVE_857, CFG)  # bus arrives long after T2 dep
    assert d.state == "missed"
    assert d.leave_in_min is None


def test_stale_bus_feed_degrades():
    """Stale feed timestamp -> degraded, timetable-only, never a confident leave-now."""
    now = at(8, 0)
    stale = at(7, 50)  # 10 min old, > MAX_FEED_STALENESS_SEC (120s)
    d = solve([bus(8, 5, 8, 20)], DARTS, now, ARRIVE_857, CFG, bus_feed_ts=stale, dart_feed_ts=now)
    assert d.degraded
    assert d.state == "degraded"
    assert "stale" in d.status_text.lower()


def test_empty_feed_degrades():
    now = at(8, 0)
    d = solve([], DARTS, now, ARRIVE_857, CFG)
    assert d.degraded and d.state == "degraded"


def test_boundary_exactly_at_buffer_is_makeable():
    """Bus arriving Howth exactly buffer minutes before the DART still makes it."""
    now = at(8, 0)
    # buffer 6; arrive 08:29 -> ready 08:35 == T1 dep 08:35 -> boardable (>=)
    d = solve([bus(8, 12, 8, 29)], DARTS, now, ARRIVE_857, CFG)
    assert d.target_train.traincode == "T1"
    assert d.state in ("comfortable", "tight")


# ---------------------------------------------------------------------------
# DepartAfter objective
# ---------------------------------------------------------------------------

def test_depart_after_picks_earliest_boardable_connection():
    """'Leave around 8' with two catchable buses -> the earliest one, not the
    latest — this is a speed objective, not a deadline one."""
    now = at(7, 30)
    buses = [
        bus(8, 5, 8, 20, route="L1"),
        bus(8, 12, 8, 27, route="L2"),
    ]
    d = solve(buses, DARTS, now, DepartAfter(earliest=time(8, 0)), CFG)
    assert d.state == "next_available"
    assert d.target_bus.depart == at(8, 5)
    assert d.target_train.traincode == "T1"


def test_depart_now_is_depart_after_now():
    """'Leaving now, what's fastest' — earliest boardable is a bus that has
    already left when 'earliest' is in the past relative to some buses."""
    now = at(8, 8)
    buses = [
        bus(8, 5, 8, 20, route="L1"),   # already departed relative to now
        bus(8, 12, 8, 27, route="L2"),  # the earliest still-catchable one
    ]
    d = solve(buses, DARTS, now, DepartAfter(earliest=now.time()), CFG)
    assert d.target_bus.depart == at(8, 12)


def test_depart_after_degrades_the_same_way_on_stale_feeds():
    now = at(8, 0)
    stale = at(7, 50)
    d = solve([bus(8, 5, 8, 20)], DARTS, now, DepartAfter(earliest=time(8, 0)), CFG, bus_feed_ts=stale, dart_feed_ts=now)
    assert d.degraded and d.state == "degraded"


def test_depart_after_no_eligible_bus_is_missed():
    now = at(8, 0)
    d = solve([bus(8, 5, 8, 20)], DARTS, now, DepartAfter(earliest=time(9, 0)), CFG)
    assert d.state == "missed"


# ---------------------------------------------------------------------------
# solve_return() — the mirror chain (DART Central->Howth, then bus Howth->home).
# Independent of whether the real return stop_id was resolved; these fixtures
# exercise the chaining logic directly against hand-built DART/bus data.
# ---------------------------------------------------------------------------

def return_dart(dep_h, dep_m, arr_h, arr_m, code=None):
    return DartService(traincode=code or f"R{dep_h}{dep_m}", depart=at(dep_h, dep_m), arrive=at(arr_h, arr_m), destination="Howth")


def return_bus(dep_h, dep_m, arr_h, arr_m, route="L1", conf="live", delay=0.0):
    return BusDeparture(
        trip_id=f"{route}_return_{dep_h}{dep_m}", route=route,
        depart=at(dep_h, dep_m), arrive=at(arr_h, arr_m),
        depart_confidence=conf, arrive_confidence=conf, delay_min=delay,
    )


def test_solve_return_picks_latest_dart_that_still_makes_arrive_by():
    now = at(17, 0)
    # R1 (17:20-17:35) -> ready 17:41 -> catches bus1 (17:45-18:00)
    # R2 (17:35-17:50) -> ready 17:56 -> catches bus2 (18:00-18:10)
    # both bus arrivals are on-time for an 18:20 deadline; prefer the later dart (R2).
    darts = [return_dart(17, 20, 17, 35, "R1"), return_dart(17, 35, 17, 50, "R2")]
    buses = [return_bus(17, 45, 18, 0), return_bus(18, 0, 18, 10)]
    d = solve_return(darts, buses, now, ArriveBy(deadline=time(18, 20)), CFG)
    assert d.state in ("comfortable", "tight")
    assert d.target_train.traincode == "R2"
    assert d.target_bus.arrive == at(18, 10)


def test_solve_return_depart_now_picks_earliest_boardable_dart():
    now = at(17, 0)
    darts = [return_dart(17, 20, 17, 35, "R1"), return_dart(17, 35, 17, 50, "R2")]
    buses = [return_bus(17, 45, 18, 0)]
    d = solve_return(darts, buses, now, DepartAfter(earliest=time(17, 0)), CFG)
    assert d.state == "next_available"
    assert d.target_train.traincode == "R1"


def test_solve_return_confidence_comes_from_the_bus_leg():
    """Only the bus feed grades confidence — solve_return() must read it off
    the bus even though the DART is the first leg chronologically."""
    now = at(17, 0)
    darts = [return_dart(17, 20, 17, 35, "R1")]
    buses = [return_bus(17, 45, 18, 0, conf="propagated", delay=3.5)]
    d = solve_return(darts, buses, now, DepartAfter(earliest=time(17, 0)), CFG)
    assert d.confidence == "propagated"
    assert d.interchange_delay_min == 3.5


def test_solve_return_degrades_on_stale_dart_feed():
    now = at(17, 0)
    stale = at(16, 50)
    d = solve_return(
        [return_dart(17, 20, 17, 35)], [return_bus(17, 45, 18, 0)], now,
        DepartAfter(earliest=time(17, 0)), CFG, dart_feed_ts=stale, bus_feed_ts=now,
    )
    assert d.degraded and d.state == "degraded"


def test_solve_return_missed_when_nothing_catchable():
    now = at(17, 0)
    # non-empty bus feed (so it's not a degraded/empty-feed case), but the
    # only bus departs before the dart's interchange-ready time -> nothing chains
    d = solve_return(
        [return_dart(17, 20, 17, 35)], [return_bus(17, 30, 17, 45)],
        now, DepartAfter(earliest=time(17, 0)), CFG,
    )
    assert d.state == "missed"


# ---------------------------------------------------------------------------
# DART-only (no configured bus leg — Route.bus_alight_stop is None)
# ---------------------------------------------------------------------------

def test_dart_only_arrive_by():
    now = at(8, 0)
    d = solve_dart_only(DARTS, now, ARRIVE_857, CFG)
    assert d.state == "comfortable"
    assert d.target_train.traincode == "T1"
    assert d.target_bus is None


def test_dart_only_depart_now():
    now = at(8, 40)
    d = solve_dart_only(DARTS, now, DepartAfter(earliest=now.time()), CFG)
    assert d.state == "next_available"
    assert d.target_train.traincode == "T2"


def test_dart_only_degrades_on_empty_feed():
    now = at(8, 0)
    d = solve_dart_only([], now, ARRIVE_857, CFG)
    assert d.degraded and d.state == "degraded"


def test_dart_only_missed_when_deadline_unreachable():
    now = at(9, 0)  # both DARTs already gone
    d = solve_dart_only(DARTS, now, ARRIVE_857, CFG)
    assert d.state == "missed"


# ---------------------------------------------------------------------------
# Logging-only mask — a caller-side concern (sync.py), not solve()'s.
# ---------------------------------------------------------------------------

def test_mask_for_logging_only_hides_the_instruction_but_keeps_the_data():
    """First-week posture: computed target stays populated (for the delay
    chart), but the human status doesn't surface an authoritative leave-now
    on an un-tuned buffer."""
    now = at(8, 0)
    honest = solve([bus(8, 5, 8, 20)], DARTS, now, ARRIVE_857, CFG)
    masked = mask_for_logging_only(honest)
    assert masked.state == "logging_only"
    assert "logging only" in masked.status_text.lower()
    assert masked.target_train is not None          # data still there for logging
    assert masked.interchange_delay_min is not None


def test_mask_for_logging_only_does_not_hide_degraded_or_missed():
    """Feed-trust signals must survive the mask — otherwise a stale-feed
    problem silently reads as 'just tuning', not 'something's actually wrong'."""
    now = at(8, 0)
    degraded = solve([], DARTS, now, ARRIVE_857, CFG)
    assert mask_for_logging_only(degraded).state == "degraded"

    missed = solve([bus(9, 0, 9, 20)], DARTS, at(8, 55), ARRIVE_857, CFG)
    assert mask_for_logging_only(missed).state == "missed"
