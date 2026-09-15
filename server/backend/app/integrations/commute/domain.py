"""Pure commute-solver domain types — config, route, objective, models. No I/O.

All datetimes are tz-naive local (Europe/Dublin) — the whole system runs in one
timezone and never crosses it, so naive-local keeps the arithmetic honest and
testable. `client.py` is responsible for converting feed timestamps into this
naive-Dublin representation before anything here sees them.
"""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional, Union

# --- config -------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    # Slack at the interchange: the bus must arrive this many minutes before
    # the train departs (or the train must arrive this many minutes before the
    # return bus departs). Tune on a fortnight of logged interchange delay
    # distribution (commute_history) — drop if reliable, push up if flaky.
    interchange_buffer_min: int = 6

    # If a feed's timestamp is older than this, the solver degrades to a
    # timetable-only, explicitly-flagged status rather than a confident
    # leave-now instruction. The trust-critical path.
    max_feed_staleness_sec: int = 120


DEFAULT = Config()


def on_date(t: time, ref: datetime) -> datetime:
    """Combine a wall-clock time with the date of `ref` (tz-naive, local)."""
    return datetime.combine(ref.date(), t)


# --- route ----------------------------------------------------------------
#
# Direction is a data parameter, not a code fork: both legs (bus, rail) are
# structurally symmetric — board somewhere, alight somewhere, connect at an
# interchange. A Route says which stop/station is which for a given direction.
#
# The two concrete Route instances used to be module constants naming this
# household's stops. They are deployment config now — built by
# `app.integrations.commute.routing.routes()`, which is where the config read
# lives so this module stays pure (no I/O).


@dataclass(frozen=True)
class Route:
    name: str                              # "outbound" | "return"
    bus_board_stop: str                    # GTFS stop_id to board the bus at
    bus_alight_stop: Optional[str]         # GTFS stop_id to alight at the interchange; None = bus leg not configured
    dart_board_station: str                # rail station code to board at
    dart_alight_station: str               # rail station code to alight at
    dart_direction_substr: str             # substring to match the rail feed's `Direction` field
    routes: tuple = ()                     # GTFS route_short_names


# --- objective ------------------------------------------------------------
#
# Separable from direction. ArriveBy is the original backward-chain ("latest
# bus that still makes the deadline"). DepartAfter is a forward search
# ("earliest boardable connection at/after this time") — since later buses
# monotonically catch later-or-equal DARTs, the earliest boardable connection
# is also the fastest one, so "leave now, optimize for speed" is just
# DepartAfter(earliest=now.time()), no separate solver branch needed.


@dataclass(frozen=True)
class ArriveBy:
    deadline: time


@dataclass(frozen=True)
class DepartAfter:
    earliest: time


Objective = Union[ArriveBy, DepartAfter]


# --- typed rows ---------------------------------------------------------------

# Confidence ordering, best -> worst. Used to take the "worst link in the chain".
CONFIDENCE_ORDER = ("live", "propagated", "extrapolated-back", "scheduled")


def worst_confidence(*labels: Optional[str]) -> str:
    present = [l for l in labels if l]
    if not present:
        return "scheduled"
    return max(
        present,
        key=lambda l: CONFIDENCE_ORDER.index(l) if l in CONFIDENCE_ORDER else len(CONFIDENCE_ORDER),
    )


@dataclass
class BusDeparture:
    """One L1/L2 trip, with predicted times at both ends of the leg that
    matters: departure at the board stop and arrival at the alight stop
    (direction-neutral — which physical stop is which is Route's job)."""

    trip_id: str
    route: str                       # "L1" | "L2"
    depart: datetime                 # predicted departure at the board stop
    arrive: datetime                 # predicted arrival at the alight stop
    depart_confidence: str = "scheduled"
    arrive_confidence: str = "scheduled"
    delay_min: Optional[float] = None  # predicted delay at the alight stop vs schedule, for logging

    @property
    def confidence(self) -> str:
        return worst_confidence(self.depart_confidence, self.arrive_confidence)


@dataclass
class DartService:
    """One DART service, with the two times the solver chains on (direction-
    neutral — Route says which station is board vs alight)."""

    traincode: str
    depart: datetime                 # departure from the board station
    arrive: datetime                 # arrival at the alight station
    destination: str = ""


@dataclass
class Decision:
    """The solver's output. An instruction, never a probability."""

    status_text: str = ""                     # the human instruction
    leave_in_min: Optional[int] = None        # minutes until the target bus leaves the board stop
    target_train: Optional[DartService] = None
    target_bus: Optional[BusDeparture] = None
    confidence: str = "scheduled"
    degraded: bool = False
    reason: str = ""                          # why degraded / why downgraded
    interchange_delay_min: Optional[float] = None    # predicted delay of target bus at the interchange, for logging

    # coarse machine-readable state for the status sensor / automations.
    # one of: comfortable | tight | next_train | next_available | missed |
    #   degraded | logging_only | idle
    state: str = "idle"

    def __str__(self) -> str:
        return self.status_text


def mask_for_logging_only(decision: Decision) -> Decision:
    """First-week posture for the *scheduled* job only: computed target stays
    populated (so logging/tuning still works) but the human status doesn't
    surface an authoritative instruction on an un-tuned buffer. Never applied
    to on-demand queries — a user who just explicitly asked deserves the
    honest answer `solve()` already computed.

    A no-op for degraded/missed decisions — those are feed-trust signals
    ("check manually"), not authoritative leave-now instructions, and hiding
    them would defeat the whole point of the deriverstion gate.
    """
    from dataclasses import replace

    if decision.state in ("degraded", "missed"):
        return decision
    return replace(decision, state="logging_only", status_text="Logging only — buffer being tuned")
