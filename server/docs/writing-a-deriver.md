# Writing a deriver

A **deriver** is an integration whose output is *computed* from state comar
already holds rather than fetched from anywhere: a forecaster, a solver, a
classifier. `type="deriver"` in the manifest, `AlgoIntegration` as the base
class, and the shared environment in `app/algo/` for everything else.

This is the companion to `writing-an-integration.md`. Read that first — the
manifest, discovery, config, capability and tool machinery are identical.
Everything below is what's *different*.

## Why there is a shared environment at all

`hardware/homeassistant/commute/` and `app/integrations/commute/` are the same
solver, forked. One ran as a pyscript shim inside Home Assistant, the other
runs here; the HA copy went stale (last touched 2026-07-16, 139 lines against
this one's 398) and nothing complained, because nothing was watching. That is
what happens when each algorithmic thing brings its own runtime, its own feed
adapters and its own config.

The parts every algo needs are identical, and none of them are the interesting
part of an algo:

| | where | the rule that matters |
|---|---|---|
| input | `app/algo/features.py` | **one** `features()`, called by both training and serving |
| models | `app/algo/estimators.py`, `artifacts.py` | fitted parameters as JSON, versioned, activated deliberately |
| output | `app/algo/predictions.py`, `sinks.py` | Postgres rows + an HA sensor + two MCP tools, from one declaration |
| judgement | `app/algo/llm.py` | `coglib.llm`, with tokens and cost on the run ledger |
| proof | `app/algo/scoring.py` | predictions graded against reality; skill against a baseline |

That last row is the reason to build a harness rather than write each algo by
hand. **An algo nobody scores is an algo being trusted for no reason** — the
same shape as a sanitiser you never verify: the check is cheap, its absence is
invisible, and the failure is silent.

## Package layout

```
app/integrations/_algo_template/
├── manifest.py    # MANIFEST — type="deriver", models=[], a training cron
├── __init__.py    # The AlgoIntegration subclass: SPEC + features() + observe()
└── training.py    # Zero-arg wrapper the manifest's training cron points at
```

Three files, not five. There's no `client.py` (a deriver reads internal state)
and usually no `models.py` — see the next section.

## A deriver owns no tables

`models=[]`. Predictions, model versions and runs live in three kernel-owned
tables (`app/models/algo.py`):

- **`algo_predictions`** — one row per (quantity, target moment, horizon), with
  `actual`/`error`/`scored_at` filled in later.
- **`algo_model_versions`** — fitted parameters as JSONB, `is_active` picking
  which one serves.
- **`algo_runs`** — one row per predict/train/score execution, including LLM
  cost. The deriver equivalent of `SyncState`.

They're kernel-owned rather than per-integration because the manifest requires
an integration's models to resolve in its own `models.py` — so
per-integration ownership would mean one predictions table per algo, and then
scoring, backtesting and the dashboard would each need per-algo code. Building
the harness was a kernel change; **adding a deriver still touches zero kernel
files**, and `tests/test_algo_harness.py::TestDeriverDropsIn` proves it the
same way `test_drop_in_integration.py` does for ordinary integrations.

Declare `models` only for state that is genuinely yours and is not a
prediction.

## The four methods

```python
class SolarForecast(AlgoIntegration):
    SPEC = AlgoSpec(
        algo="solar_forecast",                       # must equal name
        quantities=[Quantity("pv_power", unit="W")],
        horizons=[60, 180, 360, 720],
        estimator="ridge",
    )

    def features(self, session, made_at, target_at) -> dict[str, float]: ...
    def observe(self, session, quantity, at) -> float | None: ...
    def baseline(self, session, quantity, made_at, target_at): ...   # optional
    def ha_entity_for(self, quantity) -> str | None: ...             # optional
```

Everything else — the prediction cycle, the training loop, the holdout split,
artifact save/activate, the HA push, two MCP tools, the run ledger, scoring — is
inherited.

### `features()` — and the one trap to understand

It is called with a **historical** `made_at` during training and with **now**
during serving. It must return what would have been known at `made_at`.

That has a hard consequence: it may only read tables that keep history.
`ha_entities` holds the *latest* state per entity, so reading it from a feature
builder makes every training row see today's value while claiming to be last
March — and the model then scores beautifully offline and predicts nothing.
Read `homeassistant.entities`' `numeric_history()` instead, which goes to the
append-only `ha_state_changes`.

⚠️ And underneath that: `ha_state_changes` records numeric→numeric transitions
**only when `ha_record_numeric_history` is true**, and it defaults to false. On
a deployment where it has never been set there is no numeric history at all,
`train()` reports `insufficient_rows`, and that is correct behaviour rather
than a bug. Turn the flag on and wait.

The other rule: never read the target moment's own observed value. That is the
leak, and it is indistinguishable from a very good model until it goes live.

### `observe()`

Ground truth for a quantity at a moment, or `None` if not observable yet.
**Never `0.0` to mean unknown** — that's an error the full size of the
prediction, and it drags every average it touches. `None` leaves the row
unscored and retried, then written off after `scoring.ABANDON_AFTER_DAYS`
(`scored_at` set, `actual` still NULL — a distinguishable state, not a silent
drop). Without that cap, an unobservable prediction is re-queried forever.

### `baseline()`

Defaults to persistence — the last observable value. Recorded on every
prediction row **at prediction time**, so it can't be chosen afterwards to
flatter the model. Skill is `1 - model_mae / baseline_mae`; negative skill means
switch the model off, which is a conclusion raw MAE will never hand you.

## Models are JSON, never pickles

`fit()` may import scikit-learn. `predict()` may import numpy and nothing else,
and reads a dict of numbers off the model-version row.

The asymmetry is the design. Serving never touches scikit-learn, so a
scikit-learn upgrade cannot change a live prediction — and this repo already
caps every dependency's major *because* an unpinned bump broke the whole tool
surface once. A model is also then diffable in review and carried by the nightly
`pg_dump` with no extra plumbing.

The cost is real: only models whose parameters are a handful of numbers can
ship. Built in: `mean` (the floor any real model has to clear), `ridge`
(standardised, folded into the params), `bucket_mean` (per-bucket means over one
feature — cheap non-linearity, ideal for hour-of-day). Need something else?
`register_estimator()` takes one from your own package. Don't reach for a
pickle quietly.

## Three cadences, on purpose

| what | where | typical |
|---|---|---|
| predict | `MANIFEST.schedule` → `sync()` → `run_predict()` | hourly |
| train | `MANIFEST.background_tasks` → your `training.py` | weekly |
| score | kernel job `score_algo_predictions` | hourly, automatic |

Refitting on every prediction cycle is the most common mistake: it makes every
prediction unreproducible (you can no longer say which model said what), and it
lets one bad week of input data replace a working model within the hour. Hence
`save()` always writes a version **inactive**, and `activate()` is separate.

Scoring is kernel-owned so a new deriver is graded from its first prediction
without declaring anything. A per-deriver scoring cron fails silently, and a
silent scoring outage looks exactly like a working forecaster.

## Output: two consumers, two shapes

**Home Assistant** gets one `sensor.<...>` per quantity: the *state* is the next
forecast value (what an automation means by "the forecast") and the
*attributes* carry the series (what a dashboard wants). Comar computes, HA
displays and automates — the seam `hardware/docs/hardening-2026-08.md` settled
on. The series is capped at `MAX_HA_SERIES_POINTS` because HA's recorder stores
the whole attribute blob on every state change.

⚠️ Resolve the entity through `ha_entity_for()` when it is deployment config.
An entity_id like `sensor.living_room_*` in a committed `AlgoSpec` is exactly
what `tests/test_personalisation_guard.py` sweeps for, and the fix there is
always a config key, never an allowlist entry.

**Claude and the comar app** get `<algo>_forecast` and `<algo>_accuracy`,
generated from the spec so they can't drift from it. Accuracy travels *with* the
forecast on purpose: a prediction read without its track record invites more
confidence than it has earned.

The HA push is best-effort and happens after the Postgres commit — a dead HA
must not turn a good prediction cycle into an error the scheduler retries. Same
ordering as commute, for the same reason: the durable record is the product,
the sensor is a projection of it.

## LLM-shaped algos

```python
SPEC = AlgoSpec(..., llm_model="claude-haiku-4-5-20251001")
...
verdict = self.llm().ask_json(prompt)
```

Goes through `coglib.llm`, so tokens and cost land on the `AlgoRun` row and
"what did the predictive layer cost this month" is one query. Keys come from
`integration_config` (falling back to `~/.config/comar/` for local runs),
passed as an argument rather than written into `os.environ` — env mutation races
between scheduler threads and leaks the key into anything that dumps the
environment.

A prompt whose output nobody grades is exactly as untrustworthy as a model
whose predictions nobody grades. Declare quantities and horizons for it too.

## Checklist

1. Copy `app/integrations/_algo_template/` to your name, replace `__ALGO_NAME__`
   in all three files (`MANIFEST.name`, `SPEC.algo`, the `name` property, and
   the `background_tasks` dotted ref must all agree with the directory name).
2. Write `features()` and `observe()`. Re-read the trap section.
3. Set the required config via `PUT /api/integrations/<name>/config`.
4. Let it predict for a while with `estimator=None` or no activated model —
   `run_predict()` is a clean no-op without one — then train, and check
   `<algo>_accuracy` before you believe the forecast.
5. Publish to HA only once skill is positive. A forecast nobody has graded is
   not something to build an automation on.
