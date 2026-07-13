"""Weather integration — Open-Meteo forecast for the configured home location."""

import logging
from datetime import date
from typing import Any

from app.db import get_db
from app.integrations.base import BaseIntegration
from app.integrations.weather.models import WeatherCurrent, WeatherForecast
from app.integrations.weather.sync import sync_weather
from app.integrations.weather.tools import get_mcp_tools, _weather_description

logger = logging.getLogger(__name__)


class WeatherIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "weather"

    @property
    def display_name(self) -> str:
        return "Weather"

    def sync(self) -> None:
        """Fetch current weather and forecast from Open-Meteo."""
        db = get_db()
        with db.session() as session:
            sync_weather(session)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return current conditions and today's forecast for the dashboard."""
        db = get_db()
        with db.session() as session:
            current = session.query(WeatherCurrent).first()
            today_forecast = (
                session.query(WeatherForecast)
                .filter_by(date=date.today())
                .first()
            )

            if not current:
                return {"status": "no_data"}

            result: dict[str, Any] = {
                "temperature": current.temp,
                "feels_like": current.feels_like,
                "humidity": current.humidity,
                "wind_speed": current.wind_speed,
                "weather_code": current.weather_code,
                "description": _weather_description(current.weather_code),
                "precipitation": current.precipitation,
                "is_day": current.is_day,
                "fetched_at": current.fetched_at.isoformat() if current.fetched_at else None,
            }

            if today_forecast:
                result["today_high"] = today_forecast.temp_max
                result["today_low"] = today_forecast.temp_min
                result["sunrise"] = today_forecast.sunrise
                result["sunset"] = today_forecast.sunset

            return result

    def sync_schedule(self) -> str | None:
        return "*/30 * * * *"  # Every 30 minutes

    def is_configured(self) -> bool:
        return True  # No API key needed for Open-Meteo
