"""Sync logic: fetch Open-Meteo data → upsert into Postgres."""

import logging
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.integrations.weather.client import fetch_weather
from app.integrations.weather.models import WeatherCurrent, WeatherForecast

logger = logging.getLogger(__name__)


def sync_weather(session: Session) -> None:
    """Fetch weather from Open-Meteo and upsert into DB.

    - Current conditions: delete old row, insert new.
    - Daily forecast: upsert by date.
    """
    data = fetch_weather()

    # --- Current conditions (single row, replace) ---
    current = data["current"]
    session.query(WeatherCurrent).delete()
    session.add(
        WeatherCurrent(
            temp=current["temperature_2m"],
            feels_like=current["apparent_temperature"],
            humidity=current["relative_humidity_2m"],
            wind_speed=current["wind_speed_10m"],
            wind_direction=current["wind_direction_10m"],
            weather_code=current["weather_code"],
            cloud_cover=current["cloud_cover"],
            precipitation=current["precipitation"],
            is_day=bool(current["is_day"]),
            fetched_at=datetime.now(timezone.utc),
        )
    )

    # --- Daily forecast (upsert by date) ---
    daily = data["daily"]
    for i, date_str in enumerate(daily["time"]):
        forecast_date = date.fromisoformat(date_str)

        existing = (
            session.query(WeatherForecast)
            .filter_by(date=forecast_date)
            .first()
        )

        if existing:
            existing.temp_max = daily["temperature_2m_max"][i]
            existing.temp_min = daily["temperature_2m_min"][i]
            existing.weather_code = daily["weather_code"][i]
            existing.precipitation_sum = daily["precipitation_sum"][i]
            existing.wind_speed_max = daily["wind_speed_10m_max"][i]
            existing.sunrise = daily["sunrise"][i]
            existing.sunset = daily["sunset"][i]
        else:
            session.add(
                WeatherForecast(
                    date=forecast_date,
                    temp_max=daily["temperature_2m_max"][i],
                    temp_min=daily["temperature_2m_min"][i],
                    weather_code=daily["weather_code"][i],
                    precipitation_sum=daily["precipitation_sum"][i],
                    wind_speed_max=daily["wind_speed_10m_max"][i],
                    sunrise=daily["sunrise"][i],
                    sunset=daily["sunset"][i],
                )
            )

    # Clean up old forecast rows (older than today)
    session.query(WeatherForecast).filter(
        WeatherForecast.date < date.today()
    ).delete()

    session.commit()
    logger.info("Weather sync complete: current + %d forecast days", len(daily["time"]))
