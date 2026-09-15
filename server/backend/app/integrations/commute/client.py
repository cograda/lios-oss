"""Feed adapters — all the ugly, feed-specific parsing at the edge.

The solver (solver.py) never touches a protobuf or an XML tag. These two
functions turn the two live feeds into the typed rows it consumes, and carry a
feed timestamp so the solver can detect staleness. A feed-format change breaks
one function here, not the logic.

Both are Route-parameterized (see domain.py) — direction is a data lookup,
not a code fork. `fetch_bus_departures` returns `([], None)` immediately when
`route.bus_alight_stop is None` (the bus leg isn't configured for that
direction yet) rather than erroring; callers use `solve_dart_only()` for that
case instead of `solve()`.

Every feed timestamp is pinned to Europe/Dublin wall-clock (naive) so the
solver's tz-naive arithmetic stays correct regardless of the container's own
timezone (comar's scheduler runs in UTC).

The 128MB GTFS static zip is never parsed at runtime — `load_static_index_json`
reads the precomputed `data/bus_static_index.json` shipped with this package.
Regenerate it with `backend/scripts/build_commute_index.py` when NTA publishes
a new schedule.
"""

import json
import logging
import ssl
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import certifi
import httpx

from app.integrations.commute.domain import BusDeparture, Config, DartService, DEFAULT, Route

logger = logging.getLogger(__name__)

DUBLIN = ZoneInfo("Europe/Dublin")

TIMEOUT = 30.0

NTA_TRIPUPDATES_URL = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"

IRISH_RAIL_STATION_URL = "http://api.irishrail.ie/realtime/realtime.asmx/getStationDataByCodeXML"
IR_NS = {"ns": "http://api.irishrail.ie/realtime/"}

_STATIC_INDEX_PATH = Path(__file__).parent / "data" / "bus_static_index.json"
_static_index_cache: Optional[dict] = None

# The NTA endpoint is Azure API Management behind Traffic Manager, and a subset
# of its regional endpoints serve only the leaf cert, omitting the GoDaddy G2
# intermediate. certifi has the matching root but not the intermediate, and
# nothing in the stack chases the leaf's AIA URL — so verification failed on
# whichever nodes DNS happened to hand us (~43% of calls). Shipping the
# intermediate and letting OpenSSL build the chain with it fixes that
# deterministically; the trust anchor is still certifi's GoDaddy root.
# See data/nta_intermediate.pem for the full write-up.
_NTA_INTERMEDIATE_PATH = Path(__file__).parent / "data" / "nta_intermediate.pem"
_nta_ssl_context_cache: Optional[ssl.SSLContext] = None

# One retry: the failure mode was per-endpoint, so a second attempt re-resolves
# and usually lands on a different node. Kept even with the chain fixed —
# Traffic Manager can still hand out a sick endpoint for other reasons.
NTA_ATTEMPTS = 2


def _nta_ssl_context() -> ssl.SSLContext:
    """certifi's trust store plus the intermediate NTA's edge nodes omit.

    Built once and reused — loading a CA bundle per request is measurable.
    """
    global _nta_ssl_context_cache
    if _nta_ssl_context_cache is None:
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.load_verify_locations(cafile=str(_NTA_INTERMEDIATE_PATH))
        _nta_ssl_context_cache = ctx
    return _nta_ssl_context_cache


def _fetch_nta_tripupdates(api_key: str) -> bytes:
    """GET the GTFS-R TripUpdates protobuf, retrying a connect-level failure."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, NTA_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=TIMEOUT, verify=_nta_ssl_context()) as client:
                response = client.get(NTA_TRIPUPDATES_URL, headers={"x-api-key": api_key})
                response.raise_for_status()
                return response.content
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            last_exc = exc
            logger.warning(
                "NTA feed connect attempt %d/%d failed (%s); retrying",
                attempt, NTA_ATTEMPTS, type(exc).__name__,
            )
    raise last_exc  # type: ignore[misc]


def dublin_now() -> datetime:
    """The one clock everything in this integration uses — tz-naive Dublin wall-clock."""
    return datetime.now(DUBLIN).replace(tzinfo=None)


def _epoch_to_dublin_naive(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=DUBLIN).replace(tzinfo=None)


def gtfs_time_to_datetime(hhmmss: str, ref: datetime) -> datetime:
    """GTFS clock time -> datetime on ref's service day. Handles >24:00:00."""
    h, m, s = (int(x) for x in hhmmss.split(":"))
    midnight = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(hours=h, minutes=m, seconds=s)


def hhmm_to_datetime(hhmm: str, ref: datetime) -> Optional[datetime]:
    """Irish Rail 'HH:MM' -> datetime near ref. '00:00' means 'no estimate'."""
    if not hhmm or hhmm.strip() in ("", "00:00"):
        return None
    h, m = (int(x) for x in hhmm.split(":"))
    dt = ref.replace(hour=h, minute=m, second=0, microsecond=0)
    # tolerate a train shown side of midnight from ref (not expected in the
    # morning window, but keep the arithmetic robust)
    if dt < ref - timedelta(hours=6):
        dt += timedelta(days=1)
    return dt


def load_static_index_json(path: Path = _STATIC_INDEX_PATH) -> dict:
    """Load the precomputed L1/L2 static index, memoized after first read.

    Shape: {trip_id: {"route": "L1"|"L2", "stops": {stop_id: (seq, "HH:MM:SS")}}}
    — generic per-stop_id, not fixed "home"/"interchange" keys, so any Route can look
    up whichever two stop_ids it cares about.
    """
    global _static_index_cache
    if _static_index_cache is None:
        with open(path) as f:
            _static_index_cache = json.load(f)
    return _static_index_cache


def predict_stop_time(
    stu_list, target_seq: int, scheduled_dt: datetime
) -> Tuple[Optional[datetime], str, float]:
    """Predict time at target_seq from a sparse StopTimeUpdate list.

    NTA reports only a handful of near-term stops per trip, so an exact match at
    our stop is the exception. Propagate the nearest-known delay onto the static
    schedule. Returns (predicted_dt, confidence, delay_minutes).
    """
    exact = next((s for s in stu_list if s.stop_id and s.stop_sequence == target_seq), None)
    if exact is not None:
        if exact.departure.time:
            dt = _epoch_to_dublin_naive(exact.departure.time)
            return dt, "live", (dt - scheduled_dt).total_seconds() / 60
        delay = exact.departure.delay or exact.arrival.delay
        dt = scheduled_dt + timedelta(seconds=delay)
        return dt, "live", delay / 60

    before = [s for s in stu_list if s.stop_sequence and s.stop_sequence <= target_seq]
    if before:
        anchor = max(before, key=lambda s: s.stop_sequence)
        delay = anchor.departure.delay or anchor.arrival.delay
        dt = scheduled_dt + timedelta(seconds=delay)
        return dt, "propagated", delay / 60

    after = [s for s in stu_list if s.stop_sequence and s.stop_sequence > target_seq]
    if after:
        anchor = min(after, key=lambda s: s.stop_sequence)
        delay = anchor.departure.delay or anchor.arrival.delay
        dt = scheduled_dt + timedelta(seconds=delay)
        return dt, "extrapolated-back", delay / 60

    return None, "scheduled", 0.0


def fetch_bus_departures(
    api_key: str,
    now: datetime,
    route: Route,
    cfg: Config = DEFAULT,
    static_index: Optional[dict] = None,
) -> Tuple[List[BusDeparture], Optional[datetime]]:
    """Live L1/L2 departures at `route.bus_board_stop` with predicted arrival
    at `route.bus_alight_stop` per trip.

    Returns (departures, feed_timestamp). Returns ([], None) immediately if
    the route's bus leg isn't configured yet (bus_alight_stop is None).
    """
    if route.bus_alight_stop is None:
        return [], None

    from google.transit import gtfs_realtime_pb2

    if static_index is None:
        static_index = load_static_index_json()

    content = _fetch_nta_tripupdates(api_key)

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)
    feed_ts = _epoch_to_dublin_naive(feed.header.timestamp) if feed.header.timestamp else None

    out: List[BusDeparture] = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        info = static_index.get(tu.trip.trip_id)
        if info is None:
            continue
        stops = info.get("stops", {})
        board = stops.get(route.bus_board_stop)
        alight = stops.get(route.bus_alight_stop)
        if board is None or alight is None:
            continue
        board_seq, board_sched = board
        alight_seq, alight_sched = alight
        board_sched_dt = gtfs_time_to_datetime(board_sched, now)
        alight_sched_dt = gtfs_time_to_datetime(alight_sched, now)

        depart, depart_conf, _ = predict_stop_time(tu.stop_time_update, board_seq, board_sched_dt)
        arrive, arrive_conf, delay = predict_stop_time(tu.stop_time_update, alight_seq, alight_sched_dt)
        if depart is None or arrive is None:
            continue
        out.append(BusDeparture(
            trip_id=tu.trip.trip_id,
            route=info.get("route", "?"),
            depart=depart,
            arrive=arrive,
            depart_confidence=depart_conf,
            arrive_confidence=arrive_conf,
            delay_min=round(delay, 1),
        ))
    out.sort(key=lambda b: b.depart)
    return out, feed_ts


def _fetch_station(client: httpx.Client, station_code: str) -> Tuple[list, Optional[datetime]]:
    response = client.get(IRISH_RAIL_STATION_URL, params={"StationCode": station_code})
    response.raise_for_status()
    root = ET.fromstring(response.content)
    rows = []
    server_time = None
    for obj in root.findall("ns:objStationData", IR_NS):
        def field(tag):
            el = obj.find(f"ns:{tag}", IR_NS)
            return el.text.strip() if el is not None and el.text else None
        if server_time is None and field("Servertime"):
            try:
                # Irish Rail's Servertime is already Dublin wall-clock, not UTC.
                server_time = datetime.fromisoformat(field("Servertime")).replace(tzinfo=None)
            except ValueError:
                server_time = None
        rows.append({
            "traincode": field("Traincode"),
            "origin": field("Origin"),
            "destination": field("Destination"),
            "direction": field("Direction"),
            "traintype": field("Traintype"),
            "exp_arrival": field("Exparrival"),
            "sch_arrival": field("Scharrival"),
            "exp_depart": field("Expdepart"),
            "sch_depart": field("Schdepart"),
        })
    return rows, server_time


def fetch_dart_services(now: datetime, route: Route) -> Tuple[List[DartService], Optional[datetime]]:
    """DART services from `route.dart_board_station` to `route.dart_alight_station`,
    filtered to `route.dart_direction_substr`, with real board-station departure
    + alight-station arrival.

    The board station's own feed only gives an ETA there, so we query the
    alight station too and join by Traincode: board station's Expdepart (when
    it leaves) + alight station's Exparrival (when it gets in).
    """
    with httpx.Client(timeout=TIMEOUT) as client:
        board_rows, board_ts = _fetch_station(client, route.dart_board_station)
        alight_rows, _ = _fetch_station(client, route.dart_alight_station)
    alight_by_code = {r["traincode"]: r for r in alight_rows}

    out: List[DartService] = []
    for r in board_rows:
        if r["traintype"] != "DART":
            continue
        if not r["direction"] or route.dart_direction_substr not in r["direction"].lower():
            continue
        ar = alight_by_code.get(r["traincode"])
        if not ar:
            continue  # hasn't entered the alight station's board yet, or doesn't serve it
        depart = hhmm_to_datetime(r["exp_depart"] or r["sch_depart"], now)
        arrive = hhmm_to_datetime(ar["exp_arrival"] or ar["sch_arrival"], now)
        if depart is None or arrive is None:
            continue
        out.append(DartService(
            traincode=r["traincode"],
            depart=depart,
            arrive=arrive,
            destination=ar["destination"] or "",
        ))
    out.sort(key=lambda d: d.depart)
    return out, board_ts
