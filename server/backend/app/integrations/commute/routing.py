"""Build the commute's concrete Routes from deployment config.

Separated from `domain.py` on 2026-07-28 so that module stays pure (no I/O)
while the route *values* become configuration.

The solver was already direction-agnostic — `Route` has been a dataclass since
the integration was written, and `solve()`/`solve_return()` take one as a
parameter. What was hardcoded was the two instances: this household's bus
stop ids and the specific interchange/terminus station codes. Those are now
manifest config keys, so the solver ships without naming anyone's commute.

Shape of the modelled journey (unchanged, and the one real remaining
assumption): a **bus leg** and a **rail leg** meeting at a single
**interchange**, in two directions. A commute that isn't bus→rail (or that
needs more than one interchange) still doesn't fit — that's a solver rewrite,
not a config key, and it is deliberately out of scope.
"""

from __future__ import annotations

from app.integrations.commute.domain import Route
from app.plugin.config_store import plugin_config

# GTFS `Direction` substrings for each leg. These follow from which end of the
# line you're travelling towards, so they're derived rather than configured:
# outbound runs from the home stop towards the city, return comes back.
_OUTBOUND_RAIL_DIRECTION = "north"
_RETURN_RAIL_DIRECTION = "south"


def _missing(cfg) -> list[str]:
    required = (
        "commute_bus_home_stop",
        "commute_bus_interchange_stop",
        "commute_rail_interchange_station",
        "commute_rail_city_station",
    )
    return [k for k in required if not str(getattr(cfg, k, "") or "").strip()]


def is_route_configured() -> bool:
    """True iff enough config exists to build both routes."""
    return not _missing(plugin_config("commute"))


def routes() -> dict[str, Route]:
    """`{"outbound": Route, "return": Route}` from config.

    Raises `PermanentError` naming the missing keys if the route isn't
    configured — the scheduler treats that as no-retry and records it on
    SyncState, so it surfaces as a configuration problem rather than a silently
    wrong "leave now".
    """
    from app.errors import PermanentError

    cfg = plugin_config("commute")
    missing = _missing(cfg)
    if missing:
        raise PermanentError(
            "commute route is not configured: set "
            + ", ".join(missing)
            + " via PUT /api/integrations/commute/config."
        )

    bus_routes = tuple(cfg.commute_bus_routes or ())

    # The return bus may alight at a different stop id than the outbound one
    # boards at — on a divided road the two directions are distinct GTFS stops
    # a few metres apart. Falls back to the outbound stop when unset.
    home_return_stop = (
        str(cfg.commute_bus_home_return_stop or "").strip()
        or cfg.commute_bus_home_stop
    )

    outbound = Route(
        name="outbound",
        bus_board_stop=cfg.commute_bus_home_stop,
        bus_alight_stop=cfg.commute_bus_interchange_stop,
        dart_board_station=cfg.commute_rail_interchange_station,
        dart_alight_station=cfg.commute_rail_city_station,
        dart_direction_substr=_OUTBOUND_RAIL_DIRECTION,
        routes=bus_routes,
    )
    ret = Route(
        name="return",
        bus_board_stop=cfg.commute_bus_interchange_stop,
        bus_alight_stop=home_return_stop,
        dart_board_station=cfg.commute_rail_city_station,
        dart_alight_station=cfg.commute_rail_interchange_station,
        dart_direction_substr=_RETURN_RAIL_DIRECTION,
        routes=bus_routes,
    )
    return {outbound.name: outbound, ret.name: ret}


def outbound() -> Route:
    """The morning direction — the one the scheduled job solves for."""
    return routes()["outbound"]
