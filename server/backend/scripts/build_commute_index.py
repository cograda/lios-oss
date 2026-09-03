"""Regenerate the static bus index used by the commute integration.

Downloads the current NTA static GTFS zip (~128MB), extracts just the
(stop_sequence, scheduled time) at every stop any configured Route cares
about, for every trip on the configured bus routes, and writes
app/integrations/commute/data/bus_static_index.json — so the running server
never has to parse the zip itself.

**Re-run whenever NTA publishes a new schedule.** They rotate trip_id
prefixes when they do, and a stale index fails silently: live trip_ids stop
matching, `bus_count` comes back 0, and every decision degrades to
"timetable only". Diagnose with the overlap check —

    live = {live feed trip_ids}; idx = load_static_index_json()
    len(live & idx.keys())    # 0 means the index is stale, not the feed

This happened between 2026-07-14 and 2026-07-29 (index prefix 5398, live
prefixes 5762/5733/5787) and the bus leg was dead the whole time.

Routes come from config (`routing.routes()`, reading the commute_bus_*
config keys), so this needs DB access — run it inside the app container:

    docker exec lios-core python scripts/build_commute_index.py
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import httpx

# Make `app.*` importable when run as a script from server/backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.commute.routing import routes as configured_routes  # noqa: E402

GTFS_STATIC_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_All.zip"
ZIP_PATH = Path(__file__).resolve().parent.parent / "app" / "integrations" / "commute" / "data" / "GTFS_All.zip"
OUT_PATH = Path(__file__).resolve().parent.parent / "app" / "integrations" / "commute" / "data" / "bus_static_index.json"


def _read_gtfs_csv(zf: zipfile.ZipFile, name: str) -> list[dict]:
    with zf.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))


def _download_gtfs_zip(zip_path: Path) -> Path:
    """Download the GTFS zip, re-fetching whenever NTA has republished.

    Caching on mere existence was the bug that made this script unable to do
    the one job its docstring promises: a stale zip left in the container from
    a previous run meant a "rebuild" produced a fresh-looking index full of the
    same dead trip_ids. Compare Content-Length against the local file and
    re-download on any mismatch.
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    remote_size = None
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        try:
            head = client.head(GTFS_STATIC_URL)
            head.raise_for_status()
            remote_size = int(head.headers.get("content-length") or 0) or None
            published = head.headers.get("last-modified")
        except httpx.HTTPError as exc:
            print(f"HEAD failed ({exc}); falling back to unconditional download")
            published = None

        if zip_path.exists() and remote_size and zip_path.stat().st_size == remote_size:
            print(f"Local zip already matches NTA's current publish ({published}) — reusing")
            return zip_path

        if zip_path.exists():
            print(f"Local zip is stale (local {zip_path.stat().st_size}B vs remote {remote_size}B) — refetching")
        print(f"Downloading GTFS static (published {published}) …")
        with client.stream("GET", GTFS_STATIC_URL) as response:
            response.raise_for_status()
            tmp = zip_path.with_suffix(".zip.part")
            with open(tmp, "wb") as fh:
                for chunk in response.iter_bytes(1 << 20):
                    fh.write(chunk)
            tmp.replace(zip_path)
    return zip_path


def _target_stop_ids(route_map: dict) -> set[str]:
    """Every stop_id any configured Route's bus leg references."""
    ids = set()
    for route in route_map.values():
        ids.add(route.bus_board_stop)
        if route.bus_alight_stop:
            ids.add(route.bus_alight_stop)
    return ids


def build_static_index(zip_path: Path) -> dict:
    """Per trip: {"route": short_name, "stops": {stop_id: (seq, "HH:MM:SS")}}.

    A single trip only ever travels one direction, so it naturally ends up
    with only the board/alight pair for whichever Route it belongs to — no
    need to match a trip against a specific Route here, just record every
    target stop it hits and let client.py look up the pair it needs.
    """
    # Resolved once: routes() hits the config store, and the two derived sets
    # below must agree with each other.
    route_map = configured_routes()
    target_stops = _target_stop_ids(route_map)
    route_short_names = {n for route in route_map.values() for n in route.routes}
    if not target_stops or not route_short_names:
        raise SystemExit(
            "No bus stops/routes configured — set the commute_bus_* config keys "
            "before building the index."
        )

    with zipfile.ZipFile(zip_path) as zf:
        routes = _read_gtfs_csv(zf, "routes.txt")
        trips = _read_gtfs_csv(zf, "trips.txt")
        stop_times = _read_gtfs_csv(zf, "stop_times.txt")

    route_ids = {r["route_id"] for r in routes if r.get("route_short_name") in route_short_names}
    route_short = {r["route_id"]: r.get("route_short_name") for r in routes}
    our_trips = {t["trip_id"]: t for t in trips if t.get("route_id") in route_ids}

    index: dict = {}
    for st in stop_times:
        tid = st.get("trip_id")
        if tid not in our_trips:
            continue
        sid = st.get("stop_id")
        if sid not in target_stops:
            continue
        entry = index.setdefault(tid, {"route": route_short.get(our_trips[tid]["route_id"]), "stops": {}})
        entry["stops"][sid] = (int(st["stop_sequence"]), st["departure_time"] or st["arrival_time"])

    # keep only trips that hit at least two target stops (a real usable leg)
    return {t: v for t, v in index.items() if len(v["stops"]) >= 2}


def main() -> None:
    zip_path = _download_gtfs_zip(ZIP_PATH)
    index = build_static_index(zip_path)
    if not index:
        raise SystemExit(
            "Built an EMPTY index — the configured stop_ids matched no trips in "
            "the current GTFS feed. Check the commute_bus_* stop ids against "
            "stops.txt; NTA occasionally renumbers stops as well as trips."
        )
    # Surface whether trip_ids actually rotated. A "successful" rebuild that
    # produced the same prefixes means the zip didn't change — the exact
    # silent no-op that left the bus leg dead 2026-07-14 → 2026-07-29.
    old_prefixes: set[str] = set()
    if OUT_PATH.exists():
        try:
            old_prefixes = {t.split("_")[0] for t in json.loads(OUT_PATH.read_text())}
        except (ValueError, OSError):
            pass

    OUT_PATH.write_text(json.dumps(index))
    new_prefixes = {t.split("_")[0] for t in index}
    print(f"Wrote {len(index)} trips to {OUT_PATH}")
    print(f"  trip_id prefixes: {sorted(new_prefixes)[:6]}")
    if old_prefixes:
        if new_prefixes == old_prefixes:
            print("  WARNING: prefixes unchanged from the previous index — if the bus "
                  "leg was already dead, this rebuild has not fixed it.")
        else:
            print(f"  rotated: was {sorted(old_prefixes)[:6]}")


if __name__ == "__main__":
    main()
