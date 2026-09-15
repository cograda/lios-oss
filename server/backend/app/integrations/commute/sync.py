"""Commute sync — fetch both outbound feeds, solve, persist, best-effort HA push.

Runs weekday mornings 07:00-08:59 Europe/Dublin (see
CommuteIntegration.sync_schedule/sync_timezone), always for the OUTBOUND
route with an ArriveBy objective — the scheduled job's job is exactly the
morning "leave by X" instruction it always was. On-demand queries in any
direction/objective go through tools.py::handle_query instead, which doesn't
touch this table at all.

Feed failures are caught individually so one dead feed still yields an honest
degraded decision rather than aborting the whole cycle — that's deliberate
and stays. But when *both* feeds fail (or the fetch itself blows up in some
unexpected way), there's nothing honest left to solve on: raise a typed
error instead of persisting a garbage row, so `SyncState`/the scheduler see
the failure instead of a permanent "ok". PermanentError for auth-shaped
failures (a dead NTA key), TransientError for everything else (timeouts,
5xx, malformed feed) — see app/errors.py for the scheduler contract. The HA
push is still best-effort and never prevents the Postgres row from
committing (ha_pushed records whether it worked).
"""

import dataclasses
import logging
from datetime import time

import httpx
from sqlalchemy.orm import Session

from app.errors import PermanentError, TransientError
from app.integrations.commute.client import DUBLIN, dublin_now, fetch_bus_departures, fetch_dart_services
from app.integrations.commute.domain import ArriveBy, DEFAULT, Decision, mask_for_logging_only
from app.integrations.commute.routing import outbound
from app.integrations.commute.models import CommuteDecision
from app.integrations.commute.solver import solve
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)


def _classify_feed_error(exc: Exception, context: str) -> Exception:
    """Map a feed-fetch failure to TransientError or PermanentError.

    Only the bus feed is key-gated (NTA); a 401/403 there means the key is
    dead/revoked — not something a retry fixes. The DART feed (Irish Rail)
    is public, so its failures are network/format only. Everything else
    (5xx, 429, timeouts, connection errors, or a totally unexpected parsing
    bug) is treated as transient — worth a retry.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return PermanentError(f"{context}: HTTP {status} (check NTA_API_KEY)")
        return TransientError(f"{context}: HTTP {status}")
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError)):
        return TransientError(f"{context}: {exc}")
    return TransientError(f"{context}: {exc}")


def _config_from_settings():
    return dataclasses.replace(DEFAULT, interchange_buffer_min=plugin_config("commute").commute_interchange_buffer_min)


def _parse_hhmm(s: str) -> time:
    h, m = (int(x) for x in s.split(":"))
    return time(h, m)


def _push_ha_sensors(decision: Decision, now) -> tuple[bool, list[str]]:
    """Publish sensor.commute_* to HA, mirroring the retired pyscript shim's
    shapes. Best-effort — never raises; caller records the result.

    Returns (ha_pushed, failed_sensors). `ha_pushed` tracks only the primary
    sensor.commute_status — that's the one thing anything downstream (the HA
    dashboard, the morning briefing) actually reads — so one dead secondary
    sensor doesn't collapse the whole push into "failed" the way a single
    `ok &= set_state(...)` chain used to. Failed secondaries are still
    logged by name rather than silently dropped.
    """
    ha_cfg = plugin_config("homeassistant")
    if not (ha_cfg.ha_url and ha_cfg.ha_token):
        return False, []

    from app.plugin.capabilities import get_capability

    set_state = get_capability("homeassistant.entities").set_state

    train = ""
    if decision.target_train is not None:
        train = f"{decision.target_train.traincode} → {decision.target_train.arrive:%H:%M}"

    primary_ok = set_state(
        "sensor.commute_status",
        decision.state,
        {
            "friendly_name": "Commute status",
            "instruction": decision.status_text,
            "confidence": decision.confidence,
            "degraded": decision.degraded,
            "reason": decision.reason,
            "target_train": train,
            "leave_in_min": decision.leave_in_min,
            "decided_at": now.isoformat(),
            "icon": "mdi:bus-clock",
        },
    )

    failed: list[str] = []

    if not set_state(
        "sensor.commute_leave_in",
        decision.leave_in_min if decision.leave_in_min is not None else "unknown",
        {"friendly_name": "Leave in", "unit_of_measurement": "min", "icon": "mdi:walk"},
    ):
        failed.append("sensor.commute_leave_in")

    if not set_state(
        "sensor.commute_target_train",
        f"{decision.target_train.arrive:%H:%M}" if decision.target_train else "unknown",
        {"friendly_name": "Target train (destination arr)", "detail": train, "icon": "mdi:train"},
    ):
        failed.append("sensor.commute_target_train")

    if decision.interchange_delay_min is not None:
        if not set_state(
            "sensor.commute_bus_delay_interchange",
            round(decision.interchange_delay_min, 1),
            {
                "friendly_name": "Target bus delay at interchange",
                "unit_of_measurement": "min",
                "state_class": "measurement",
                "icon": "mdi:timer-alert",
            },
        ):
            failed.append("sensor.commute_bus_delay_interchange")

    if not primary_ok:
        failed.insert(0, "sensor.commute_status")

    if failed:
        logger.warning("Commute: HA push partial failure — sensors failed: %s", ", ".join(failed))

    return primary_ok, failed


def _aware(dt):
    """client.py returns naive Dublin wall-clock timestamps; the
    bus_feed_ts/dart_feed_ts columns are tz-aware, so attach Dublin tzinfo
    before persisting (Postgres/psycopg normalizes on write)."""
    return dt.replace(tzinfo=DUBLIN) if dt is not None else None


def sync_commute(session: Session) -> None:
    """One solve cycle: fetch both outbound feeds, solve, persist, best-effort HA push.

    One dead feed still yields an honest degraded decision (deliberate — see
    module docstring). Both feeds dead means there's nothing to solve on: no
    row is persisted, and a typed error propagates so the scheduler records
    the failure instead of a silent "ok".
    """
    now = dublin_now()
    commute_cfg = plugin_config("commute")
    cfg = _config_from_settings()
    objective = ArriveBy(deadline=_parse_hhmm(commute_cfg.commute_arrive_by))

    # Raises PermanentError if the route isn't configured — no-retry, recorded
    # on SyncState, so an unconfigured deployment shows a config problem rather
    # than solving someone else's commute.
    route = outbound()

    bus_exc: Exception | None = None
    buses, bus_ts = [], None
    try:
        buses, bus_ts = fetch_bus_departures(commute_cfg.nta_api_key, now, route, cfg)
    except Exception as e:
        logger.exception("Commute: failed to fetch bus feed")
        bus_exc = e

    dart_exc: Exception | None = None
    darts, dart_ts = [], None
    try:
        darts, dart_ts = fetch_dart_services(now, route)
    except Exception as e:
        logger.exception("Commute: failed to fetch DART feed")
        dart_exc = e

    if bus_exc is not None and dart_exc is not None:
        bus_err = _classify_feed_error(bus_exc, "bus feed")
        dart_err = _classify_feed_error(dart_exc, "DART feed")
        message = f"Commute: both feeds failed — bus: {bus_exc}; dart: {dart_exc}"
        if isinstance(bus_err, PermanentError) and isinstance(dart_err, PermanentError):
            raise PermanentError(message) from bus_exc
        raise TransientError(message) from bus_exc

    decision = solve(buses, darts, now, objective, cfg, bus_feed_ts=bus_ts, dart_feed_ts=dart_ts)
    if not commute_cfg.commute_surface_decisions:
        decision = mask_for_logging_only(decision)

    row = CommuteDecision(
        state=decision.state,
        status_text=decision.status_text,
        leave_in_min=decision.leave_in_min,
        confidence=decision.confidence,
        degraded=decision.degraded,
        reason=decision.reason or None,
        target_bus_trip_id=decision.target_bus.trip_id if decision.target_bus else None,
        target_bus_route=decision.target_bus.route if decision.target_bus else None,
        target_bus_dep_home=decision.target_bus.depart if decision.target_bus else None,
        target_bus_arr_interchange=decision.target_bus.arrive if decision.target_bus else None,
        target_train_code=decision.target_train.traincode if decision.target_train else None,
        target_train_interchange_dep=decision.target_train.depart if decision.target_train else None,
        target_train_dest_arr=decision.target_train.arrive if decision.target_train else None,
        interchange_delay_min=decision.interchange_delay_min,
        bus_feed_ts=_aware(bus_ts),
        dart_feed_ts=_aware(dart_ts),
        bus_count=len(buses),
        dart_count=len(darts),
    )

    try:
        row.ha_pushed, _failed_sensors = _push_ha_sensors(decision, now)
    except Exception:
        logger.exception("Commute: HA push failed unexpectedly")
        row.ha_pushed = False

    session.add(row)
    session.commit()
