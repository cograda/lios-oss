"""Open-Meteo API client.

The location is deployment config (`weather_latitude` / `weather_longitude` /
`weather_timezone` / `weather_location_name` in the manifest) — it used to be
a hardcoded lat/lon for the household's home town.
"""

import logging

import httpx

from app.plugin.config_store import plugin_config
from app.plugin.sync_runtime import classify_exc

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

_BASE_PARAMS = {
    "current": ",".join([
        "temperature_2m",
        "relative_humidity_2m",
        "apparent_temperature",
        "precipitation",
        "weather_code",
        "cloud_cover",
        "wind_speed_10m",
        "wind_direction_10m",
        "is_day",
    ]),
    "daily": ",".join([
        "weather_code",
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_sum",
        "wind_speed_10m_max",
        "sunrise",
        "sunset",
    ]),
    "forecast_days": 7,
}


def location() -> dict:
    """This deployment's configured weather location.

    Read per call, not at import: config lives in the DB, and importing this
    module must not require a database (tool schemas are built without one).

    Raises `PermanentError` if unconfigured — the scheduler classifies that as
    no-retry and records it on SyncState, so it shows up as a configuration
    problem on the dashboard rather than as a silently wrong forecast.
    """
    from app.errors import PermanentError

    cfg = plugin_config("weather")
    missing = [
        key for key, val in (
            ("weather_latitude", cfg.weather_latitude),
            ("weather_longitude", cfg.weather_longitude),
        )
        if not str(val).strip()
    ]
    if missing:
        raise PermanentError(
            "weather location is not configured: set "
            f"{' and '.join(missing)} via PUT /api/integrations/weather/config "
            f"(or the HOME_{missing[0].upper()} env fallback)."
        )

    try:
        lat, lon = float(cfg.weather_latitude), float(cfg.weather_longitude)
    except (TypeError, ValueError) as e:
        raise PermanentError(
            f"weather location is not numeric: latitude={cfg.weather_latitude!r} "
            f"longitude={cfg.weather_longitude!r}"
        ) from e

    return {
        "latitude": lat,
        "longitude": lon,
        "timezone": cfg.weather_timezone,
        "name": cfg.weather_location_name,
    }


def params() -> dict:
    """Full Open-Meteo query params for the configured location."""
    loc = location()
    return {
        **_BASE_PARAMS,
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
        "timezone": loc["timezone"],
    }


def fetch_weather() -> dict:
    """Fetch current weather and 7-day forecast from Open-Meteo.

    Returns the raw API response dict with 'current' and 'daily' keys.
    Raises a classified TransientError/PermanentError (app.errors) on HTTP
    or network errors — Open-Meteo needs no API key, so there's no oauth or
    "needs re-auth" concept here, just transient vs permanent.
    """
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(OPEN_METEO_URL, params=params())
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        raise classify_exc(exc, "Open-Meteo fetch_weather") from exc

    logger.info(
        "Fetched weather: %.1f°C, code %d",
        data["current"]["temperature_2m"],
        data["current"]["weather_code"],
    )
    return data
