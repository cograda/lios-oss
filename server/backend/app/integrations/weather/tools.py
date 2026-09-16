"""MCP tool definitions and handlers for Weather."""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.weather.models import WeatherCurrent, WeatherForecast
from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config
from app.tools import CustomTool, ToolAnnotations

logger = logging.getLogger(__name__)

# How stale the local outdoor sensor's HA state is allowed to be before
# falling back to Open-Meteo (issue #155). 30 minutes: HA's own poll/WS
# reconcile cadence is far tighter than this, so anything older than it means
# the sensor (or HA itself) has actually stopped reporting, not just that it
# hasn't ticked yet — DS18B20 readings barely move minute to minute.
_OUTDOOR_SENSOR_MAX_AGE = timedelta(minutes=30)

# States HA writes that are not a reading at all, never coerced to a number.
_NOT_A_READING = {None, "unknown", "unavailable"}


def _outdoor_sensor_temperature(session: Session) -> tuple[float, dict] | None:
    """A fresh outdoor-temperature reading from the configured HA entity, or
    None if unconfigured, unknown to HA, unavailable, or stale.

    Returns `(temp, meta)` — `meta` carries the entity id and its last-known
    timestamp, folded into `handle_current`'s `temperature_source` block so
    the brief can say *why* it trusts (or doesn't) this number.
    """
    entity_id = (plugin_config("weather").weather_outdoor_sensor_entity_id or "").strip()
    if not entity_id:
        return None

    ha = get_capability("homeassistant.entities")
    entity = ha.entity_state(session, entity_id)
    if entity is None:
        return None

    state = entity.get("state")
    if state in _NOT_A_READING:
        return None
    try:
        temp = float(state)
    except (TypeError, ValueError):
        return None

    # last_changed is when the value itself last moved; synced_at is HA's
    # sync heartbeat and is bumped even when the state didn't change. Prefer
    # last_changed (a true "still reporting" signal) and fall back to
    # synced_at only if the entity has never recorded a change.
    stamp = entity.get("last_changed") or entity.get("synced_at")
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - stamp
    if age > _OUTDOOR_SENSOR_MAX_AGE:
        return None

    return temp, {"entity_id": entity_id, "as_of": stamp.isoformat()}

# WMO Weather interpretation codes
# https://open-meteo.com/en/docs#weathervariables
WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _weather_description(code: int) -> str:
    """Convert WMO weather code to human-readable description."""
    return WMO_CODES.get(code, f"Unknown ({code})")


def handle_current(session: Session, arguments: dict[str, Any]) -> str:
    """Return current weather conditions."""
    current = session.query(WeatherCurrent).first()

    if not current:
        return json.dumps({"error": "No weather data available. Try syncing first."})

    # Also grab today's forecast for sunrise/sunset
    today_forecast = (
        session.query(WeatherForecast)
        .filter_by(date=date.today())
        .first()
    )

    result = {
        "temperature": current.temp,
        "feels_like": current.feels_like,
        "humidity": current.humidity,
        "wind_speed": current.wind_speed,
        "wind_direction": current.wind_direction,
        "weather_code": current.weather_code,
        "description": _weather_description(current.weather_code),
        "cloud_cover": current.cloud_cover,
        "precipitation": current.precipitation,
        "is_day": current.is_day,
        "fetched_at": current.fetched_at.isoformat() if current.fetched_at else None,
        "temperature_source": "open_meteo",
        "temperature_source_label": "(Open-Meteo)",
        "units": {
            "temperature": "°C",
            "wind_speed": "km/h",
            "precipitation": "mm",
            "humidity": "%",
            "cloud_cover": "%",
        },
    }

    # Prefer a fresh local outdoor-sensor reading over Open-Meteo's
    # interpolated one for "current temperature" (issue #155). Only
    # `temperature` is ever overridden — `feels_like` has no local
    # equivalent, and the forecast below always stays Open-Meteo. Never let
    # this override cost the caller the weather: any failure here (HA
    # unconfigured, capability boundary hiccup, unexpected data shape) falls
    # straight back to Open-Meteo's number.
    try:
        override = _outdoor_sensor_temperature(session)
    except Exception:
        logger.warning("outdoor sensor temperature override failed", exc_info=True)
        override = None
    if override is not None:
        temp, meta = override
        result["temperature"] = temp
        result["temperature_source"] = "gate_sensor"
        result["temperature_source_label"] = "(gate sensor)"
        result["temperature_source_entity_id"] = meta["entity_id"]
        result["temperature_source_as_of"] = meta["as_of"]

    if today_forecast:
        result["today"] = {
            "temp_max": today_forecast.temp_max,
            "temp_min": today_forecast.temp_min,
            "sunrise": today_forecast.sunrise,
            "sunset": today_forecast.sunset,
        }

    return json.dumps(result, indent=2)


def handle_forecast(session: Session, arguments: dict[str, Any]) -> str:
    """Return 7-day weather forecast."""
    days = min(int(arguments.get("days", 7)), 7)

    forecasts = (
        session.query(WeatherForecast)
        .filter(WeatherForecast.date >= date.today())
        .order_by(WeatherForecast.date.asc())
        .limit(days)
        .all()
    )

    if not forecasts:
        return json.dumps({"error": "No forecast data available. Try syncing first."})

    result = []
    for f in forecasts:
        result.append({
            "date": f.date.isoformat(),
            "temp_max": f.temp_max,
            "temp_min": f.temp_min,
            "weather_code": f.weather_code,
            "description": _weather_description(f.weather_code),
            "precipitation_sum": f.precipitation_sum,
            "wind_speed_max": f.wind_speed_max,
            "sunrise": f.sunrise,
            "sunset": f.sunset,
            "units": {
                "temperature": "°C",
                "wind_speed": "km/h",
                "precipitation": "mm",
            },
        })

    return json.dumps(result, indent=2)


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        CustomTool(
            name="weather_current",
            description=(
                "Current weather conditions at the configured location. "
                "Returns temperature, feels-like, humidity, wind, cloud cover, "
                "precipitation, and a human-readable description. "
                "Includes today's sunrise/sunset and high/low."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=handle_current,
            annotations=ToolAnnotations(
                read_only_hint=True, idempotent_hint=True, open_world_hint=True,
            ),
            category="home",
            examples=[
                "What's the weather like?",
                "Is it raining?",
                "What temperature is it?",
            ],
        ).build(),
        CustomTool(
            name="weather_forecast",
            description=(
                "7-day weather forecast for the configured location. "
                "Returns daily high/low temperatures, precipitation, wind, "
                "sunrise/sunset, and weather description. "
                "Use this to plan ahead for the week."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": ["integer", "string"],
                        "description": "Number of forecast days to return (1-7, default 7).",
                        "default": 7,
                    },
                },
            },
            handler=handle_forecast,
            annotations=ToolAnnotations(
                read_only_hint=True, idempotent_hint=True, open_world_hint=True,
            ),
            category="home",
            examples=[
                "What's the weather this week?",
                "Will it rain tomorrow?",
                "Weekend weather forecast",
            ],
        ).build(),
    ]
