"""Manifest for `solar_forecast` — the first real deriver.

`type="deriver"`, so `app.algo.AlgoIntegration` is the base class and the
kernel's hourly `score_algo_predictions` job grades it automatically. Owns no
tables: predictions, model versions and runs live in the shared kernel tables.

Every entity id is deployment config rather than a committed default. That is
not ceremony — `tests/test_personalisation_guard.py` sweeps committed defaults
for household-specific strings, and an entity id is exactly that.
"""

from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, StalenessProbe, TaskSpec

MANIFEST = IntegrationManifest(
    name="solar_forecast",
    display_name="Solar Forecast",
    version="1.0.0",
    type="deriver",
    description=(
        "Forecasts PV generation from the inverter's own recent output plus the "
        "incumbent forecast, scored against what the panels actually produced."
    ),
    icon="Sun",
    models=[],
    embedding_sources=[],
    # Nothing outward: every input is already in comar's HA cache.
    reads_from=[],
    writes_to=[],
    # Every 20 minutes past the hour. Frequent enough that the one-hour horizon
    # is genuinely fresh; not so frequent that the prediction table grows faster
    # than anything reads it.
    #
    # The kernel scores predictions at :12 (`score_algo_predictions`). Until
    # 2026-09-02 that 52-minute gap was the ONLY thing keeping grades honest:
    # scoring used a fixed 5-minute grace while observe() needs 15, so a
    # scorer running at :25 would have graded the :20 cycle's rows on a
    # half-window mean. `SPEC.score_grace_min` now carries the constraint, so
    # the two crons may move; they no longer have to stay apart.
    schedule="20 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    # 6 hours: three missed cycles. Deliberately not tighter — the lesson from
    # commute's manifest is that a probe tighter than the schedule's own quiet
    # periods trains everyone to ignore system_alerts.
    staleness_probe=StalenessProbe(
        model="AlgoPrediction",
        timestamp_column="made_at",
        # Narrowed to this deriver's own rows. Every deriver writes to the same
        # shared table, so an unfiltered MAX(made_at) would report the freshest
        # row across all of them — one live forecaster silently masking a dead
        # one, the same failure shape `per_user` exists to prevent.
        filter_column="algo",
        filter_value="solar_forecast",
        threshold_minutes=6 * 60,
    ),
    background_tasks=[
        TaskSpec(
            name="solar_forecast_train",
            target="app.integrations.solar_forecast.training:run_training",
            kind="cron",
            # Weekly, Sunday 04:40. Refitting per prediction cycle would make
            # every prediction unreproducible and would let one overcast week
            # replace a working model within the hour.
            cron="40 4 * * sun",
            misfire_grace_time=600,
        ),
    ],
    routes=[],
    config_schema={
        "pv_power_entity": ConfigFieldSpec(
            type="str",
            required=True,
            description=(
                "HA entity for PV DC input power in W — the thing being "
                "forecast AND the ground truth it is scored against. Use the "
                "inverter's PV input rather than its AC active power: on a "
                "hybrid inverter, active power also carries battery "
                "charge/discharge, so forecasting it means forecasting "
                "household behaviour as well as the weather."
            ),
        ),
        "incumbent_power_entity": ConfigFieldSpec(
            type="str",
            default="",
            description=(
                "HA entity carrying the existing forecast's estimate of power "
                "*now*, in W (e.g. Forecast.Solar's). Used as a feature: at any "
                "past moment its recorded value is what that forecast believed "
                "then, which is legitimately knowable at prediction time."
            ),
        ),
        "incumbent_next_hour_entity": ConfigFieldSpec(
            type="str",
            default="",
            description=(
                "HA entity carrying the existing forecast's next-hour energy "
                "estimate, in kWh. The strongest single feature available, and "
                "the reason this deriver is worth running at all: it can learn "
                "that forecast's local bias."
            ),
        ),
        "incumbent_remaining_today_entity": ConfigFieldSpec(
            type="str",
            default="",
            description=(
                "HA entity for the existing forecast's remaining-today energy, "
                "in kWh. A day-level signal, so it helps the longer horizons "
                "where the next-hour estimate says nothing."
            ),
        ),
        "publish_entity": ConfigFieldSpec(
            type="str",
            default="",
            description=(
                "Entity to publish the forecast to, e.g. "
                "sensor.solar_pv_forecast. Empty means do not publish — the "
                "right setting until the accuracy tool shows positive skill, "
                "so nobody automates on a forecast nobody has graded."
            ),
        ),
        "min_solar_elevation_deg": ConfigFieldSpec(
            type="int",
            default=0,
            description=(
                "Skip target moments where the sun is below this elevation. "
                "Nights are trivially zero on both sides, so including them "
                "makes MAE look excellent and skill look like nothing — the "
                "metrics stop meaning anything. 0 = horizon."
            ),
        ),
    },
    oauth=None,
    provides=[],
    # `homeassistant.entities` for numeric history (features + ground truth) and
    # for publishing the sensor; `weather.query` only for the configured
    # latitude/longitude the solar-elevation filter needs. Reading another
    # integration's config via plugin_config() is the same pattern commute uses
    # for ha_url/ha_token.
    depends_on=["homeassistant.entities", "weather.query"],
)
