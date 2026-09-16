# Next-day total kWh — research, not production

Hand-run / cron-run material for a **separate question** from the shipped
`solar_forecast` deriver: not "what will output look like at a given hour
1–12h out" (the live `pv_power` quantity, `estimator="ridge"`), but "at
~23:00 tonight, what will TOTAL production be tomorrow" — asked for
decisions like "run the dishwasher tomorrow afternoon" or "charge off solar
vs. grid tomorrow".

**Nothing here is imported by any deployed path.** Not a `type="deriver"`
integration, no manifest, no HA publish, no MCP tool. This is deliberately
true while the design is still being validated — see the caveats below.

## Why this isn't just a new quantity on the existing `SolarForecastIntegration`

`AlgoSpec.estimator` is one setting for the whole spec, and the live hourly
model already commits to `estimator="ridge"`. This question wants a
Bayesian estimator (posterior mean + predictive uncertainty, Forecast.Solar's
own "tomorrow" estimate as the prior, weather-forecast features layered on
as evidence) without touching or risking the proven hourly model. Once this
is validated, promoting it into a real second deriver (own `AlgoSpec`, an
estimator registered via `app.algo.estimators.register_estimator` from this
integration's own package, per `writing-a-deriver.md`) is the right move —
just not yet.

## Data source: HA long-term statistics, not comar's own mirror

comar's `ha_backfill_history` only reaches HA's raw recorder history
(`purge_keep_days`, default ~10 days) — it has no path to HA's separate
long-term statistics store, which is where a `state_class` sensor's history
actually survives indefinitely. `forecast_capture.yaml` (house/homeassistant,
added 2026-08-15) already snapshots the day-ahead weather forecast nightly
into template sensors carrying `state_class` for exactly this reason. This
research code talks to HA's statistics websocket API directly
(`recorder/statistics_during_period`) rather than going through comar, since
comar has no code path for it yet.

Fields it reads (all knowable the night before the target day):

| field | HA entity | note |
|---|---|---|
| `actual_kwh` (target) | `sensor.inverter_daily_yield` | resets daily; HA's per-day `change` stat IS that day's total, not `max`/`sum` — confirmed live, don't assume |
| `solar_forecast_tomorrow_kwh` | `sensor.solar_forecast_tomorrow` | Forecast.Solar's own next-day estimate — the Bayesian prior |
| `fc_cloud_pct` / `fc_temp_max_c` / `fc_temp_min_c` / `fc_wind_kmh` | `sensor.forecast_tomorrow_*` | day-ahead weather forecast, captured by `forecast_capture.yaml` |

## Known limitations (2026-09-12 first pass, 28 rows)

- **Every row so far is from one unusually sunny, warm stretch.** The model
  has learned this summer's regime and has seen zero autumn/winter cloud
  cover or day length. Do not trust it across a season change until it's
  actually seen one.
- **16 training rows / 5 features is small enough to overfit on noise** —
  `fc_wind_kmh` had ~zero raw correlation with the target (r=0.04) but got a
  non-trivial fitted coefficient. Don't read individual coefficients as real
  until there's more data.
- Test-set MAE lost to plain climatology once (2.77 vs. the model's 3.84) —
  plausibly a small, unusually calm test window rather than a real failure,
  but recorded rather than hidden. Re-check as the eval log grows.
- Forecast.Solar's own estimate is consistently biased low vs. actual across
  the whole window (mean 20.5 kWh predicted vs. 31.7 actual) — this is the
  bias the model exists to correct, and it held up: skill vs. the raw
  incumbent forecast alone was consistently +60-70%.

## Files

- `build_dataset.py` — pulls HA long-term stats, appends any new complete day
  to `dataset.json` (idempotent on date — safe to re-run).
- `fit_model.py` — chronological 60/20/20 train/val/test split, fits
  `sklearn.linear_model.BayesianRidge` on standardized features, prints
  summary stats + per-split MAE vs. two baselines (raw incumbent forecast,
  train-set climatology) + appends one line to `eval_log.md`.
- `dataset.json` — gitignored (generated data, not config) — the growing
  dataset itself.
- `eval_log.md` — gitignored — one line per re-run, so skill over time is
  visible without re-running history.
- `daily_update.sh` — what the launchd job actually calls: sources HA
  credentials, runs both scripts in sequence.

## The daily job

`~/Library/LaunchAgents/ie.comar.solar-daily-research.plist`, installed on
this Mac (not the deploy server — this uses `core/server/backend/.venv`,
which only exists here). Fires at **00:20 local**, just after midnight —
deliberately not 23:00-ish same-day, so the day that just ended is fully
finalized in HA's long-term statistics before querying it (its forecast
"prior" feature comes from the PREVIOUS night's 23:00 `forecast_capture`
snapshot, already sitting in HA stats by then regardless). Logs to
`research/launchd.log`/`launchd.err.log` in this directory.

⚠️ **Known fragility, stated rather than hidden**: this only runs while the
Mac is on and awake, same class of limitation already documented elsewhere
in this repo for laptop-only automation. If this model gets promoted to a
real deriver, it should move to the production scheduler (`app/scheduler.py`,
manifest-driven cron), which runs on the always-on server instead.

Check it's alive: `launchctl list | grep comar-solar` should show a PID or a
recent non-negative last exit code. Remove it:
`launchctl unload ~/Library/LaunchAgents/ie.comar.solar-daily-research.plist`.
