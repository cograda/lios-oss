"""MCP tool definitions and handlers for Weather."""

import json
import logging
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.weather.models import WeatherCurrent, WeatherForecast
from app.tools import CustomTool, ToolAnnotations

logger = logging.getLogger(__name__)

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
        "units": {
            "temperature": "°C",
            "wind_speed": "km/h",
            "precipitation": "mm",
            "humidity": "%",
            "cloud_cover": "%",
        },
    }

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
