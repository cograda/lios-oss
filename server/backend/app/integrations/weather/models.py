"""SQLAlchemy models for cached weather data."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class WeatherCurrent(Base):
    """Current weather conditions. Single row, overwritten each sync."""

    __tablename__ = "weather_current"

    id: Mapped[int] = mapped_column(primary_key=True)
    temp: Mapped[float] = mapped_column(Float)
    feels_like: Mapped[float] = mapped_column(Float)
    humidity: Mapped[int] = mapped_column(Integer)
    wind_speed: Mapped[float] = mapped_column(Float)
    wind_direction: Mapped[int] = mapped_column(Integer)
    weather_code: Mapped[int] = mapped_column(Integer)
    cloud_cover: Mapped[int] = mapped_column(Integer)
    precipitation: Mapped[float] = mapped_column(Float)
    is_day: Mapped[bool] = mapped_column(Boolean)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<WeatherCurrent {self.temp}°C code={self.weather_code}>"


class WeatherForecast(Base):
    """Daily weather forecast. One row per day, 7 days."""

    __tablename__ = "weather_forecast"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    temp_max: Mapped[float] = mapped_column(Float)
    temp_min: Mapped[float] = mapped_column(Float)
    weather_code: Mapped[int] = mapped_column(Integer)
    precipitation_sum: Mapped[float] = mapped_column(Float)
    wind_speed_max: Mapped[float] = mapped_column(Float)
    sunrise: Mapped[str | None] = mapped_column(nullable=True)
    sunset: Mapped[str | None] = mapped_column(nullable=True)

    def __repr__(self) -> str:
        return f"<WeatherForecast {self.date} {self.temp_min}-{self.temp_max}°C>"
