"""The connection solver — pure, no I/O. The one part with real thinking in it.

Physical model (kept deliberately literal so it can't drift from reality):
  1. You board the first leg: depart at `depart`, alight at `arrive`.
  2. At the interchange you board the first second-leg service departing
     >= first-leg arrival + buffer.
  3. That service reaches its destination at `arrive`.

`solve()` is the bus-then-rail chain (the outbound direction: home ->
interchange -> destination). `solve_return()` is the rail-then-bus chain
(destination -> interchange -> home).
They're not just argument-order swaps of one generic function: only the bus
feed (NTA GTFS-RT) grades confidence/delay per stop — Irish Rail's realtime
feed doesn't — so whichever function's first-leg type is a DartService reads
confidence/delay off the *bus* leg regardless of chain position. Both share
the interchange-buffer chaining primitive (`_caught`) and objective branches
that only ever touch the common `depart`/`arrive` fields.

Two objectives:
  - ArriveBy(deadline): the recommended first-leg departure is the *latest*
    that still lands by the deadline — leave as late as possible, arrive on
    time. When a slip means that departure can no longer catch the ideal
    (latest) connection, the recommendation falls back to an earlier
    departure catching an earlier, safer connection, and the status says so.
  - DepartAfter(earliest): the recommended first-leg departure is the
    *earliest* boardable connection at/after `earliest` — since later
    departures monotonically catch later-or-equal connections, this is also
    the fastest connection, so "leave now" is just DepartAfter(earliest=now).

Output is always an instruction, never a probability.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from app.integrations.commute.domain import (
    ArriveBy,
    BusDeparture,
    Config,
    DartService,
    DEFAULT,
    Decision,
    DepartAfter,
    Objective,
    on_date,
)

# Slack (minutes) beyond the required buffer below which we call a plan "tight"
# rather than "comfortable". Only meaningful for the ArriveBy objective.
TIGHT_SLACK_MIN = 2


def _caught(first_leg_arrive: datetime, second_leg: List, buffer_min: int):
    """The first second-leg service physically boardable after the first
    leg's arrival plus the interchange buffer. Only touches `.depart` —
    works for either BusDeparture or DartService as `second_leg`."""
    ready_at = first_leg_arrive + timedelta(minutes=buffer_min)
    boardable = [x for x in second_leg if x.depart >= ready_at]
    if not boardable:
        return None
    return min(boardable, key=lambda x: x.depart)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _deriverstion_reason(
    first_leg: List,
    second_leg: List,
    now: datetime,
    cfg: Config,
    first_leg_ts: Optional[datetime],
    second_leg_ts: Optional[datetime],
    first_leg_label: str,
    second_leg_label: str,
) -> Optional[str]:
    stale = []
    if first_leg_ts is not None and (now - first_leg_ts).total_seconds() > cfg.max_feed_staleness_sec:
        stale.append(first_leg_label)
    if second_leg_ts is not None and (now - second_leg_ts).total_seconds() > cfg.max_feed_staleness_sec:
        stale.append(second_leg_label)
    if not first_leg:
        stale.append(f"{first_leg_label}(empty)")
    if not second_leg:
        stale.append(f"{second_leg_label}(empty)")
    if not stale:
        return None
    return f"stale/empty feeds: {', '.join(stale)}"


def solve(
    buses: List[BusDeparture],
    darts: List[DartService],
    now: datetime,
    objective: Objective,
    cfg: Config = DEFAULT,
    bus_feed_ts: Optional[datetime] = None,
    dart_feed_ts: Optional[datetime] = None,
) -> Decision:
    """Chain a bus + onward DART into a single instruction for `objective`
    (the outbound direction: home -> interchange -> destination)."""
    # --- deriverstion gate (trust-critical): never instruct on stale/empty feeds ---
    reason = _deriverstion_reason(buses, darts, now, cfg, bus_feed_ts, dart_feed_ts, "bus", "DART")
    if reason:
        return Decision(
            status_text="Feeds stale — timetable only, check manually",
            degraded=True,
            reason=reason,
            state="degraded",
        )

    # For each future bus, the onward service it actually catches (first
    # boardable, any). NB monotonicity: a later bus arrives later, so catches
    # a later-or-equal service. There is therefore no "fall back to an
    # earlier train" under ArriveBy — if the deadline-making service is
    # unreachable, earlier ones are even harder; you fall *forward* to a
    # later one and arrive late. That's the real recovery signal.
    future_buses = [b for b in buses if b.depart > now]
    caught_pairs = [(b, _caught(b.arrive, darts, cfg.interchange_buffer_min)) for b in future_buses]
    caught_pairs = [(b, d) for b, d in caught_pairs if d is not None]

    if not caught_pairs:
        return Decision(
            status_text="No catchable bus reaches a connecting service — check manually",
            state="missed",
            reason="no future bus catches any connecting service",
        )

    if isinstance(objective, ArriveBy):
        return _solve_arrive_by(caught_pairs, now, objective, cfg)
    return _solve_depart_after(caught_pairs, now, objective)


def solve_return(
    darts: List[DartService],
    buses: List[BusDeparture],
    now: datetime,
    objective: Objective,
    cfg: Config = DEFAULT,
    dart_feed_ts: Optional[datetime] = None,
    bus_feed_ts: Optional[datetime] = None,
) -> Decision:
    """Chain a DART + onward bus into a single instruction for `objective`
    (the return direction: destination -> interchange -> home).

    Structurally the mirror of solve(), but confidence/delay reporting always
    comes off the bus leg (the only feed that grades it), regardless of
    whether the bus is the first or second leg of the chain.
    """
    reason = _deriverstion_reason(darts, buses, now, cfg, dart_feed_ts, bus_feed_ts, "DART", "bus")
    if reason:
        return Decision(
            status_text="Feeds stale — timetable only, check manually",
            degraded=True,
            reason=reason,
            state="degraded",
        )

    future_darts = [d for d in darts if d.depart > now]
    caught_pairs = [(d, _caught(d.arrive, buses, cfg.interchange_buffer_min)) for d in future_darts]
    caught_pairs = [(d, b) for d, b in caught_pairs if b is not None]

    if not caught_pairs:
        return Decision(
            status_text="No catchable DART reaches a connecting bus — check manually",
            state="missed",
            reason="no future DART catches any connecting bus",
        )

    if isinstance(objective, ArriveBy):
        return _solve_return_arrive_by(caught_pairs, now, objective, cfg)
    return _solve_return_depart_after(caught_pairs, now, objective)


def _solve_arrive_by(caught_pairs, now: datetime, objective: ArriveBy, cfg: Config) -> Decision:
    arrive_by = on_date(objective.deadline, now)
    on_time = [(b, d) for b, d in caught_pairs if d.arrive <= arrive_by]

    if on_time:
        # leave as late as possible: latest-departing bus that still makes the deadline
        bus, caught = max(on_time, key=lambda bd: bd[0].depart)
        leave_in = max(0, round((bus.depart - now).total_seconds() / 60))
        ready_at = bus.arrive + timedelta(minutes=cfg.interchange_buffer_min)
        slack_min = round((caught.depart - ready_at).total_seconds() / 60)

        decision = Decision(
            leave_in_min=leave_in,
            target_train=caught,
            target_bus=bus,
            confidence=bus.confidence,
            interchange_delay_min=bus.delay_min,
        )
        leave_phrase = "Leave now" if leave_in <= 1 else f"Leave in {leave_in} min"
        train_phrase = f"{_fmt(bus.depart)} {bus.route} → {_fmt(caught.arrive)}"
        if slack_min <= TIGHT_SLACK_MIN:
            decision.state = "tight"
            decision.status_text = f"{leave_phrase} — {train_phrase} — tight ({slack_min} min spare)"
        else:
            decision.state = "comfortable"
            decision.status_text = f"{leave_phrase} — {train_phrase} — comfortable"
        return decision

    # Can't make the deadline with any bus — surface the least-late option so
    # the damage is known, not hidden. Minimise arrival time.
    bus, caught = min(caught_pairs, key=lambda bd: bd[1].arrive)
    leave_in = max(0, round((bus.depart - now).total_seconds() / 60))
    late_min = round((caught.arrive - arrive_by).total_seconds() / 60)
    decision = Decision(
        leave_in_min=leave_in,
        target_train=caught,
        target_bus=bus,
        confidence=bus.confidence,
        interchange_delay_min=bus.delay_min,
        state="next_train",
        reason="no bus makes the deadline",
    )
    leave_phrase = "Leave now" if leave_in <= 1 else f"Leave in {leave_in} min"
    late_note = f"~{late_min} min late" if late_min > 0 else "on time"
    decision.status_text = (
        f"Next service — {leave_phrase}, {_fmt(bus.depart)} {bus.route} "
        f"→ {_fmt(caught.arrive)} ({late_note})"
    )
    return decision


def _solve_depart_after(caught_pairs, now: datetime, objective: DepartAfter) -> Decision:
    earliest = on_date(objective.earliest, now)
    eligible = [(b, d) for b, d in caught_pairs if b.depart >= earliest]
    if not eligible:
        return Decision(
            status_text="No catchable bus departs at or after that time — check manually",
            state="missed",
            reason="no future bus catches a connecting service at/after the requested time",
        )

    # earliest-departing boardable connection at/after `earliest` — by the
    # monotonicity invariant this is also the fastest connection available.
    bus, caught = min(eligible, key=lambda bd: bd[0].depart)
    leave_in = max(0, round((bus.depart - now).total_seconds() / 60))

    decision = Decision(
        leave_in_min=leave_in,
        target_train=caught,
        target_bus=bus,
        confidence=bus.confidence,
        interchange_delay_min=bus.delay_min,
        state="next_available",
    )
    leave_phrase = "Leave now" if leave_in <= 1 else f"Leave in {leave_in} min"
    decision.status_text = f"{leave_phrase} — {_fmt(bus.depart)} {bus.route} → {_fmt(caught.arrive)}"
    return decision


def _solve_return_arrive_by(caught_pairs, now: datetime, objective: ArriveBy, cfg: Config) -> Decision:
    """caught_pairs: [(DartService, BusDeparture)] — dart is the boardable
    first leg, bus is the connecting second leg you actually arrive home on."""
    arrive_by = on_date(objective.deadline, now)
    on_time = [(d, b) for d, b in caught_pairs if b.arrive <= arrive_by]

    if on_time:
        # board the DART as late as possible while still making the deadline
        dart, bus = max(on_time, key=lambda db: db[0].depart)
        leave_in = max(0, round((dart.depart - now).total_seconds() / 60))
        ready_at = dart.arrive + timedelta(minutes=cfg.interchange_buffer_min)
        slack_min = round((bus.depart - ready_at).total_seconds() / 60)

        decision = Decision(
            leave_in_min=leave_in,
            target_train=dart,
            target_bus=bus,
            confidence=bus.confidence,
            interchange_delay_min=bus.delay_min,
        )
        leave_phrase = "Leave now" if leave_in <= 1 else f"Leave in {leave_in} min"
        journey_phrase = f"{_fmt(dart.depart)} → {_fmt(dart.arrive)}, {bus.route} → {_fmt(bus.arrive)}"
        if slack_min <= TIGHT_SLACK_MIN:
            decision.state = "tight"
            decision.status_text = f"{leave_phrase} — {journey_phrase} — tight ({slack_min} min spare)"
        else:
            decision.state = "comfortable"
            decision.status_text = f"{leave_phrase} — {journey_phrase} — comfortable"
        return decision

    dart, bus = min(caught_pairs, key=lambda db: db[1].arrive)
    leave_in = max(0, round((dart.depart - now).total_seconds() / 60))
    late_min = round((bus.arrive - arrive_by).total_seconds() / 60)
    decision = Decision(
        leave_in_min=leave_in,
        target_train=dart,
        target_bus=bus,
        confidence=bus.confidence,
        interchange_delay_min=bus.delay_min,
        state="next_train",
        reason="no DART makes the deadline",
    )
    leave_phrase = "Leave now" if leave_in <= 1 else f"Leave in {leave_in} min"
    late_note = f"~{late_min} min late" if late_min > 0 else "on time"
    decision.status_text = (
        f"Next service — {leave_phrase}, {_fmt(dart.depart)} → {_fmt(dart.arrive)}, "
        f"{bus.route} → {_fmt(bus.arrive)} ({late_note})"
    )
    return decision


def _solve_return_depart_after(caught_pairs, now: datetime, objective: DepartAfter) -> Decision:
    earliest = on_date(objective.earliest, now)
    eligible = [(d, b) for d, b in caught_pairs if d.depart >= earliest]
    if not eligible:
        return Decision(
            status_text="No catchable DART departs at or after that time — check manually",
            state="missed",
            reason="no future DART catches a connecting bus at/after the requested time",
        )

    dart, bus = min(eligible, key=lambda db: db[0].depart)
    leave_in = max(0, round((dart.depart - now).total_seconds() / 60))

    decision = Decision(
        leave_in_min=leave_in,
        target_train=dart,
        target_bus=bus,
        confidence=bus.confidence,
        interchange_delay_min=bus.delay_min,
        state="next_available",
    )
    leave_phrase = "Leave now" if leave_in <= 1 else f"Leave in {leave_in} min"
    decision.status_text = f"{leave_phrase} — {_fmt(dart.depart)} → {_fmt(dart.arrive)}, {bus.route} → {_fmt(bus.arrive)}"
    return decision


def solve_dart_only(
    darts: List[DartService],
    now: datetime,
    objective: Objective,
    cfg: Config = DEFAULT,
    dart_feed_ts: Optional[datetime] = None,
) -> Decision:
    """Same objective semantics as solve(), for a Route with no configured bus
    leg (Route.bus_alight_stop is None) — reports the DART service alone
    (target_bus stays None) rather than a bus+DART chain. A permanently-empty
    bus feed isn't a "stale feed" problem here, so this has its own
    deriverstion gate that only looks at the DART feed.
    """
    stale = []
    if dart_feed_ts is not None and (now - dart_feed_ts).total_seconds() > cfg.max_feed_staleness_sec:
        stale.append("DART")
    if not darts:
        stale.append("DART(empty)")
    if stale:
        return Decision(
            status_text="Feeds stale — timetable only, check manually",
            degraded=True,
            reason=f"stale/empty feeds: {', '.join(stale)}",
            state="degraded",
        )

    future = [d for d in darts if d.depart > now]
    if not future:
        return Decision(
            status_text="No upcoming service — check manually",
            state="missed",
            reason="no future service",
        )

    if isinstance(objective, ArriveBy):
        arrive_by = on_date(objective.deadline, now)
        on_time = [d for d in future if d.arrive <= arrive_by]
        if on_time:
            d = max(on_time, key=lambda x: x.depart)
            state, note = "comfortable", "comfortable"
        else:
            d = min(future, key=lambda x: x.arrive)
            late_min = round((d.arrive - arrive_by).total_seconds() / 60)
            state = "next_train"
            note = f"~{late_min} min late" if late_min > 0 else "on time"
        leave_in = max(0, round((d.depart - now).total_seconds() / 60))
        leave_phrase = "Leave now" if leave_in <= 1 else f"Leave in {leave_in} min"
        return Decision(
            leave_in_min=leave_in, target_train=d, state=state,
            status_text=f"{leave_phrase} — {_fmt(d.depart)} → {_fmt(d.arrive)} — {note}",
        )

    earliest = on_date(objective.earliest, now)
    eligible = [d for d in future if d.depart >= earliest]
    if not eligible:
        return Decision(
            status_text="No service departs at or after that time — check manually",
            state="missed",
            reason="no future service at/after the requested time",
        )
    d = min(eligible, key=lambda x: x.depart)
    leave_in = max(0, round((d.depart - now).total_seconds() / 60))
    leave_phrase = "Leave now" if leave_in <= 1 else f"Leave in {leave_in} min"
    return Decision(
        leave_in_min=leave_in, target_train=d, state="next_available",
        status_text=f"{leave_phrase} — {_fmt(d.depart)} → {_fmt(d.arrive)}",
    )
