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
    },
    oauth=None,
    provides=["weather.query"],  # facade: app.integrations.weather.facade — consumed by system
    depends_on=[],
)
