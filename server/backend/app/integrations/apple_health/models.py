"""SQLAlchemy models for Apple Health integration."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin, UserOwnedMixin


class HealthDailyMetric(UserOwnedMixin, Base):
    """One row per user/date/metric_type — upserted on each push."""

    __tablename__ = "health_daily_metrics"
    __table_args__ = (
        UniqueConstraint("user_id", "date", "metric_type", name="uq_health_daily"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    metric_type: Mapped[str] = mapped_column(String(50))
    # steps, distance_km, active_energy_kcal, resting_hr_bpm,
    # hr_min_bpm, hr_avg_bpm, hr_max_bpm, hrv_ms
    value: Mapped[float] = mapped_column(Float)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HealthWorkout(UserOwnedMixin, SourcedRecordMixin, Base):
    """Individual workout records, keyed by HKObject UUID."""

    __tablename__ = "health_workouts"
    __table_args__ = (
        UniqueConstraint("user_id", "uid", name="uq_health_workout_user_uid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(255), index=True)
    workout_type: Mapped[str] = mapped_column(String(100))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float] = mapped_column(Float)
    distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    active_energy_kcal: Mapped[float] = mapped_column(Float, default=0.0)
    avg_heart_rate_bpm: Mapped[float] = mapped_column(Float, default=0.0)
    # NULL for every row synced from the Health Auto Export push (it carries
    # no such field); `health_workout_add` (issue #185) is the only writer
    # that sets these, so their presence itself marks a manually-entered row.
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # synced_at inherited from SourcedRecordMixin


class HealthSleepSession(UserOwnedMixin, SourcedRecordMixin, Base):
    """Sleep analysis samples — one row per stage per night."""

    __tablename__ = "health_sleep_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "uid", name="uq_health_sleep_user_uid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(255), index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stage: Mapped[str] = mapped_column(String(50))
    # inBed, asleepCore, asleepDeep, asleepREM, awake
    duration_hours: Mapped[float] = mapped_column(Float)
    # synced_at inherited from SourcedRecordMixin
