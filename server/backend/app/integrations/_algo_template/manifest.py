"""Manifest for the `_algo_template` deriver scaffold.

Read this alongside `app/integrations/_template/manifest.py`, which documents
every field an ordinary integration uses. This file only covers what is
*different* about a deriver:

  - `type="deriver"` — output computed from state comar already holds rather
    than fetched from anywhere. It selects `app.algo.AlgoIntegration` as the
    base class, and the kernel's hourly `score_algo_predictions` job walks
    exactly the derivers.
  - `models=[]` — a deriver owns no tables. Its predictions, model versions
    and runs live in the three kernel-owned tables (`app/models/algo.py`), so
    scoring, backtesting and the dashboard are written once rather than per
    algo. Declare models here only for state that is genuinely yours and is
    not a prediction.
  - `schedule` drives the *prediction* cycle (`sync()` → `run_predict()`).
  - `background_tasks` carries the *training* cycle, on its own much slower
    cron. Separate because they genuinely differ: predict often, train rarely.
  - `depends_on` names the capabilities the features are read through. A
    deriver's inputs are internal, so this is where the real dependency graph
    of the predictive layer shows up — and `app.plugin.validate` checks it is
    acyclic.

Leading underscore: `discover_integrations()`/`discover_manifests()` skip any
`app/integrations/*` whose name starts with `_`, so this package is never
imported, registered or scheduled in a real run. Copy the directory, rename
it, and replace `__ALGO_NAME__` everywhere it appears (`MANIFEST.name`,
`SPEC.algo`, the `name` property, and the dotted refs below — all four must
agree with the directory name, and `app.plugin.validate` plus
`AlgoIntegration.__init__` check that at boot).
"""

from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, StalenessProbe, TaskSpec

MANIFEST = IntegrationManifest(
    name="__ALGO_NAME__",
    display_name="Algo Template",
    version="1.0.0",
    type="deriver",
    description=(
        "Scaffold deriver: forecasts a numeric Home Assistant sensor from its "
        "own recent history."
    ),
    icon="TrendingUp",
    # No models of its own — see the module docstring.
    models=[],
    embedding_sources=[],
    # A deriver reads internal state, so it names no external systems. If yours
    # does fetch a feed (a weather API for features, say), declare it here —
    # this field is the security-relevant declaration of what talks outward.
    reads_from=[],
    # Writing a `sensor.*` back into HA goes through the
    # `homeassistant.entities` capability, which is an internal call to another
    # integration rather than a direct outbound write, so it belongs in
    # `depends_on` below rather than here.
    writes_to=[],
    # Prediction cycle. Hourly is a reasonable default: often enough that the
    # nearest horizon is fresh, rare enough that the prediction table does not
    # grow faster than anything reads it.
    schedule="20 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    # Probe the shared predictions table, filtered by nothing — a deriver that
    # has not written a prediction in three cycles is broken. Threshold spans
    # the longest legitimate gap, per the lesson in commute's manifest: a
    # threshold tighter than the schedule's own quiet periods trains everyone
    # to ignore system_alerts.
    staleness_probe=StalenessProbe(
        model="AlgoPrediction",
        timestamp_column="made_at",
        # Narrowed to this deriver's own rows. Every deriver writes to the same
        # shared table, so an unfiltered MAX(made_at) would report the freshest
        # row across all of them — one live forecaster silently masking a dead
        # one, the same failure shape `per_user` exists to prevent.
        filter_column="algo",
        filter_value="__ALGO_NAME__",
        threshold_minutes=6 * 60,
    ),
    background_tasks=[
        TaskSpec(
            name="__ALGO_NAME___train",
            target="app.integrations.__ALGO_NAME__.training:run_training",
            kind="cron",
            # Weekly. Refitting on every prediction cycle is the most common
            # mistake here: it makes every prediction unreproducible (you can
            # no longer say which model said what) and it lets one bad week of
            # input data replace a working model within the hour.
            cron="40 4 * * sun",
            misfire_grace_time=600,
        ),
    ],
    routes=[],
    config_schema={
        "source_entity": ConfigFieldSpec(
            type="str",
            required=True,
            description=(
                "Home Assistant entity_id whose numeric state this forecasts, "
                "e.g. sensor.living_room_temperature."
            ),
        ),
        "publish_entity": ConfigFieldSpec(
            type="str",
            default="",
            description=(
                "Entity to publish the forecast to. Empty means do not publish "
                "— useful while a new model is being evaluated, so nobody "
                "builds an automation on a forecast you are about to change."
            ),
        ),
    },
    oauth=None,
    provides=[],
    # Features and ground truth are read through HA's cached state. Declared,
    # so the dependency is visible and validated rather than implicit in an
    # import.
    depends_on=["homeassistant.entities"],
)
