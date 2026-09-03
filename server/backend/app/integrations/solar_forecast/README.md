# solar_forecast

Forecasts PV generation (`pv_power`, W) 1–12 hours ahead and is graded hourly
against what the panels actually produced. First real tenant of the
`app/algo/` harness — see `server/docs/writing-a-deriver.md` for the framework.

## What it actually does

It does **not** replace Forecast.Solar. It consumes it. Forecast.Solar's
recorded estimate is the strongest single feature here, and the thing this
deriver can learn that a generic model cannot is that forecast's **local**
bias: this roof's pitch and azimuth, the tree line, the inverter clipping at
5.5 kW. A generic irradiance model knows the sky; it does not know that the
chimney shades string 2 until ten in the morning.

Whether that's worth running is a measured question, not an assumption —
`solar_accuracy` answers it. If skill is negative, leave `publish_entity`
empty and stop.

## ⚠️ Setup order matters

It will do nothing at all until steps 1–3 are done, and its silence is
*correct* rather than broken — `train()` reports `insufficient_rows` and
`run_predict()` is a clean no-op with no active model.

**1. Allowlist the entities for numeric history.** Features come from
`ha_state_changes`, which keeps numeric→numeric transitions only for entities in
`homeassistant.ha_numeric_history_entities` (empty by default; the global
`ha_record_numeric_history` firehose is the wrong tool for four sensors).

```
PUT /api/integrations/homeassistant/config
{"ha_numeric_history_entities": [
   "sensor.inverter_input_power",
   "sensor.power_production_now",
   "sensor.energy_next_hour",
   "sensor.energy_production_today_remaining"
]}
```

**2. Backfill from HA's own recorder.** comar starts keeping that history from
the moment of step 1; HA has been keeping it all along. Without this the
forecaster is blind for its whole 60-day training window.

```
ha_backfill_history            # MCP tool; idempotent, re-run to top up
```

⚠️ Bounded by HA's `purge_keep_days` (default 10), so this buys days, not
months. Expect to wait a couple of weeks for a good fit either way.

**3. Configure this integration.**

```
PUT /api/integrations/solar_forecast/config
{"pv_power_entity": "sensor.inverter_input_power",
 "incumbent_power_entity": "sensor.power_production_now",
 "incumbent_next_hour_entity": "sensor.energy_next_hour",
 "incumbent_remaining_today_entity": "sensor.energy_production_today_remaining",
 "min_solar_elevation_deg": 5,
 "publish_entity": ""}
```

**4. Train, then read the accuracy before believing anything.** Training runs
weekly (Sun 04:40) or on demand. `save()` writes the version *inactive* and
`activate()` is separate, so a bad fit is a row nobody reads.

**5. Only then set `publish_entity`.** A forecast nobody has graded is not
something to build an automation on.

## Design notes

**Why the inverter's PV input, not its AC active power.** On a hybrid inverter
active power also carries battery charge/discharge, so forecasting it means
forecasting household behaviour as well as the weather. `sensor.inverter_*_power`
comes from `hardware/solar-gateway/` — the Pi that bridges the inverter's own
Wi-Fi AP to the house LAN — so this deriver is downstream of that being alive.

**Why climatology is the baseline, not persistence.** `AlgoIntegration`'s
default baseline is persistence, and "as much as right now, in six hours' time"
is nearly meaningless for solar — beating it would prove nothing. The baseline
here is the mean observed output at this hour of day over the trailing
fortnight: computable at any historical moment, and the honest bar (knowing the
time of year and time of day, and nothing about the weather).

**Why Forecast.Solar is a feature and not the baseline.** A baseline must be
knowable at prediction time. Forecast.Solar's *curve* would qualify — it is a
forecast — but comar's HA cache stores only its scalar `now`/`next hour`
sensors, not the hourly series in its attributes. So at `made_at` we can read
what it believed about `made_at` and `made_at + 1h`, and nothing about six hours
out. If those attributes ever land in the cache, the curve becomes the right
baseline and this note is the reason to switch.

**Why nights are skipped.** They are trivially zero on both sides, so including
them makes MAE look excellent and skill look like nothing — the metrics stop
meaning anything. `min_solar_elevation_deg` filters both the training grid and
the prediction cycle. Solar elevation is computed in `solar.py` from the
latitude/longitude `weather` is already configured with (twenty lines of
trigonometry rather than a `pvlib` dependency in every deployed image).

**No weather features.** `weather_forecast` is daily-only, has no cloud cover,
and is upserted by date — so a historical forecast cannot be reconstructed, and
a feature built on it would violate `features()`'s contract (it must return what
was knowable at `made_at`). The incumbent forecast's recorded sensors carry the
weather signal instead, which is the whole reason they are worth having.
