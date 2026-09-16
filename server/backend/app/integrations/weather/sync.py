"""Pull/store split for weather — V4 chunk 4.3 batch A.

Splits the old `sync_weather()` (fetch -> upsert, all in one) into the two
pieces `app.plugin.bases.SourceIntegration.sync()` calls separately:
`pull_weather` (fetch only, no DB writes) and `store_weather` (persist
only, no outbound I/O). `WeatherIntegration.pull`/`.store` in `__init__.py`
are thin one-line adapters onto these.
"""

import logging
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.integrations.weather.client import fetch_weather
from app.integrations.weather.models import WeatherCurrent, WeatherForecast
from app.plugin.bases import PullResult

logger = logging.getLogger(__name__)


def pull_weather(session: Session, cursor: str | None) -> PullResult:
    """Fetch current conditions + 7-day forecast from Open-Meteo. No DB
    writes — see `store_weather`. `cursor` is unused (Open-Meteo has no
    resumable window — every sync re-fetches the full current+7-day payload)."""
    data = fetch_weather()
    return PullResult(records=[data])


def store_weather(session: Session, records: list[dict]) -> int:
    """Upsert `records[0]` (the single Open-Meteo response payload).

    - Current conditions: delete old row, insert new.
    - Daily forecast: upsert by date.
    """
    if not records:
        return 0

    data = records[0]

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
    return 1 + len(daily["time"])
