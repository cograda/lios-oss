from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest

MANIFEST = IntegrationManifest(
    name="weather",
    display_name="Weather",
    version="1.0.0",
    type="source",
    description="Open-Meteo current conditions + 7-day forecast for the configured location (no API key required).",
    icon="Cloud",
    models=["WeatherCurrent", "WeatherForecast"],
    embedding_sources=[],
    reads_from=["open-meteo-api"],
    writes_to=[],
    schedule="*/30 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=30,  # services/freshness.py: 1800s
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    # Location is deployment config, not a code constant (2026-07-28).
    # Deliberately NOT `required`: these tools read cached rows, so they stay
    # useful without config. Only `sync()` needs a location, and it raises a
    # PermanentError naming the missing keys — which surfaces on the dashboard
    # and in system_alerts via SyncState. Gating the whole integration off
    # would have removed working read tools to enforce a write-path concern.
    config_schema={
        "weather_latitude": ConfigFieldSpec(
            type="str", default="",
            description="Decimal latitude for the forecast, e.g. 53.1459.",
        ),
        "weather_longitude": ConfigFieldSpec(
            type="str", default="",
            description="Decimal longitude for the forecast, e.g. -6.0633.",
        ),
        "weather_timezone": ConfigFieldSpec(
            type="str", default="UTC",
            description="IANA timezone for sunrise/sunset, e.g. Europe/Dublin.",
        ),
        "weather_location_name": ConfigFieldSpec(
            type="str", default="",
            description="Human-readable place name, used only in tool output.",
        ),
        # Added for issue #155: prefer a local outdoor sensor's live reading
        # over Open-Meteo's for "current temperature" — a HA entity reads the
        # actual garden/gate air, Open-Meteo interpolates a grid cell. Only
        # `temperature` is overridden this way; the forecast always stays
        # Open-Meteo (no local sensor predicts tomorrow). Default names the
        # gate-sensor board's DS18B20 probe (things/gate-sensor/), the one
        # fitted outdoor sensor in the fleet as of 2026-09-07 — not verified
        # against the live HA entity registry, so double check it if the
        # board is ever renamed or re-adopted (see contracts/fleet.md).
        "weather_outdoor_sensor_entity_id": ConfigFieldSpec(
            type="str", default="sensor.gate_sensor_temperature",
            description=(
                "HA entity id for a local outdoor-temperature sensor. When "
                "its state is a fresh number, weather_current reports it "
                "instead of Open-Meteo's current temperature. Blank disables "
                "the override entirely."
            ),
        ),
    },
    oauth=None,
    provides=["weather.query"],  # facade: app.integrations.weather.facade — consumed by system
    # homeassistant.entities: reads the outdoor-sensor entity's live state for
    # weather_current's temperature override (issue #155). Read-only — never
    # writes back, unlike commute's use of the same capability.
    depends_on=["homeassistant.entities"],
)
