"""Weather integration — Open-Meteo forecast for the configured location.

`SourceIntegration` conversion (V4 chunk 4.3, batch A). No multi-account
concept — `accounts()`/`account_user_id()`/`account_label()` stay at their
`SourceIntegration` defaults. `sync()` itself is entirely inherited; this
class supplies only `pull()`/`store()`, tool wiring, and dashboard data.
"""

import logging
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.weather.models import WeatherCurrent, WeatherForecast
from app.integrations.weather.sync import pull_weather, store_weather
from app.integrations.weather.tools import get_mcp_tools, _weather_description
from app.plugin.bases import PullResult, SourceIntegration

logger = logging.getLogger(__name__)


class WeatherIntegration(SourceIntegration):
    @property
    def name(self) -> str:
        return "weather"

    @property
    def display_name(self) -> str:
        return "Weather"

    def pull(self, account: Any, session: Session, cursor: str | None) -> PullResult:
        return pull_weather(session, cursor)

    def store(self, session: Session, records: list[dict]) -> int:
        return store_weather(session, records)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return current conditions and today's forecast for the dashboard."""
        from app.db import get_db

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

    # is_configured(): default (empty config_schema -> vacuously True; no
    # API key needed for Open-Meteo).
