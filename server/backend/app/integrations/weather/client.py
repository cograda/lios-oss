"""Open-Meteo API client for the configured home location."""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Home location — set HOME_WEATHER_LATITUDE / HOME_WEATHER_LONGITUDE
LATITUDE = settings.weather_latitude
LONGITUDE = settings.weather_longitude
TIMEZONE = "Europe/Dublin"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

PARAMS = {
    "latitude": LATITUDE,
    "longitude": LONGITUDE,
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
    "timezone": TIMEZONE,
    "forecast_days": 7,
}


def fetch_weather() -> dict:
    """Fetch current weather and 7-day forecast from Open-Meteo.

    Returns the raw API response dict with 'current' and 'daily' keys.
    Raises on HTTP or network errors.
    """
    with httpx.Client(timeout=15.0) as client:
        response = client.get(OPEN_METEO_URL, params=PARAMS)
        response.raise_for_status()
        data = response.json()

    logger.info(
        "Fetched weather: %.1f°C, code %d",
        data["current"]["temperature_2m"],
        data["current"]["weather_code"],
    )
    return data
