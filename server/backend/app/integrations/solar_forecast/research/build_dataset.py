"""Pull HA long-term statistics and append any new complete day to dataset.json.

Idempotent on date: re-running (daily, by cron) only adds days not already
present. Never rewrites an existing row, so a re-run can't silently change a
day's recorded feature values out from under an already-fitted model's eval.

See README.md for why this reads HA's statistics websocket API directly
rather than going through comar's own history mirror.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websockets

HERE = Path(__file__).parent
DATASET_PATH = HERE / "dataset.json"

STATISTIC_IDS = [
    "sensor.forecast_tomorrow_cloud",
    "sensor.forecast_tomorrow_temp_max",
    "sensor.forecast_tomorrow_temp_min",
    "sensor.forecast_tomorrow_wind",
    "sensor.solar_forecast_tomorrow",
    "sensor.inverter_daily_yield",
]

FEATURES = ["solar_forecast_tomorrow_kwh", "fc_cloud_pct", "fc_temp_max_c", "fc_temp_min_c", "fc_wind_kmh"]
TARGET = "actual_kwh"


async def fetch_stats(days: int = 65) -> dict:
    ha_url = os.environ["HA_URL"].replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    ha_token = os.environ["HA_TOKEN"]
    async with websockets.connect(ha_url, max_size=None) as ws:
        msg = json.loads(await ws.recv())
        assert msg["type"] == "auth_required", msg
        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        msg = json.loads(await ws.recv())
        if msg["type"] != "auth_ok":
            raise RuntimeError(f"HA auth failed: {msg}")

        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await ws.send(json.dumps({
            "id": 1,
            "type": "recorder/statistics_during_period",
            "start_time": start,
            "statistic_ids": STATISTIC_IDS,
            "period": "day",
        }))
        resp = json.loads(await ws.recv())
        if not resp.get("success"):
            raise RuntimeError(f"HA statistics query failed: {resp}")
        return resp["result"]


def day_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def rows_from_stats(result: dict) -> list[dict]:
    by_day: dict[str, dict] = {}
    for stat_id, rows in result.items():
        for row in rows:
            by_day.setdefault(day_key(row["start"]), {})[stat_id] = row

    records = []
    for d in sorted(by_day):
        row = by_day[d]
        yield_row = row.get("sensor.inverter_daily_yield")
        if yield_row is None:
            continue
        actual_kwh = yield_row.get("change")
        if actual_kwh is None:
            continue

        prior_day = (datetime.fromisoformat(d) - timedelta(days=1)).date().isoformat()
        prior = by_day.get(prior_day, {})

        def mean_of(stat_id):
            r = prior.get(stat_id)
            return r.get("mean") if r else None

        rec = {
            "date": d,
            TARGET: round(actual_kwh, 3),
            "solar_forecast_tomorrow_kwh": mean_of("sensor.solar_forecast_tomorrow"),
            "fc_cloud_pct": mean_of("sensor.forecast_tomorrow_cloud"),
            "fc_temp_max_c": mean_of("sensor.forecast_tomorrow_temp_max"),
            "fc_temp_min_c": mean_of("sensor.forecast_tomorrow_temp_min"),
            "fc_wind_kmh": mean_of("sensor.forecast_tomorrow_wind"),
        }
        if all(v is not None for v in rec.values()):
            records.append(rec)
    return records


def load_dataset() -> dict:
    if DATASET_PATH.exists():
        return json.loads(DATASET_PATH.read_text())
    return {"train": [], "val": [], "test": []}


def all_rows(dataset: dict) -> list[dict]:
    return dataset["train"] + dataset["val"] + dataset["test"]


def resplit(rows: list[dict]) -> dict:
    """Chronological 60/20/20 split, recomputed on the full row set each time.

    Not incremental-append-to-test: as the set grows, the split boundaries
    shift, which is correct — "test" should stay "the most recent ~20%",
    not permanently pin today's test rows as test forever while train grows
    around them.
    """
    rows = sorted(rows, key=lambda r: r["date"])
    n = len(rows)
    n_train = max(1, int(n * 0.6))
    n_val = max(1, int(n * 0.2))
    return {
        "train": rows[:n_train],
        "val": rows[n_train:n_train + n_val],
        "test": rows[n_train + n_val:],
    }


def main():
    dataset = load_dataset()
    existing_dates = {r["date"] for r in all_rows(dataset)}

    result = asyncio.run(fetch_stats())
    fetched = rows_from_stats(result)
    new_rows = [r for r in fetched if r["date"] not in existing_dates]

    if not new_rows:
        print(f"no new complete days (already have {len(existing_dates)} rows)")
        return

    merged = all_rows(dataset) + new_rows
    dataset = resplit(merged)
    DATASET_PATH.write_text(json.dumps(dataset, indent=2))

    print(f"added {len(new_rows)} new row(s): {[r['date'] for r in new_rows]}")
    print(f"dataset now: train={len(dataset['train'])} val={len(dataset['val'])} test={len(dataset['test'])}")


if __name__ == "__main__":
    main()
