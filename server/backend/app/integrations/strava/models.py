"""SQLAlchemy models for the Strava integration."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin, UserOwnedMixin


class StravaActivity(UserOwnedMixin, SourcedRecordMixin, Base):
    """One row per Strava activity, keyed by Strava's own activity id.

    `UserOwnedMixin`, not household-shared. Strava is a personal account:
    Alex and Sam each authorise their own athlete, each gets their own
    `OAuthToken` row, and neither should see the other's rides in a tool
    result. The uniqueness constraint is therefore composite
    (`user_id`, `strava_id`) — a bare `strava_id` unique constraint would be
    *almost* right, since Strava ids are globally unique, and would then
    silently drop the second athlete's copy of a ride they did together
    (Strava issues each participant their own activity id, but a shared
    upload or a future group-activity feature must not be able to collide).

    Deliberately a table of its own rather than rows in `health_workouts`:

    - `health_workouts` is `apple_health`'s model, declared in that
      integration's manifest. An integration owns its tables; writing into
      another's is the coupling the V4 kernel exists to prevent.
    - Strava carries fields Apple Health has no concept of — power, gear,
      segment efforts, the route polyline. Flattening to the common subset
      would discard exactly the data that makes a Strava archive worth
      keeping.

    Units are stored **as Strava returns them** — metres, seconds, metres per
    second — and converted at the tool boundary, never on the way in. A
    conversion applied during ingest is unrecoverable if it turns out to have
    been wrong; a conversion applied on read is a one-line fix.
    """

    __tablename__ = "strava_activities"
    __table_args__ = (
        UniqueConstraint("user_id", "strava_id", name="uq_strava_activity_user_id"),
        Index("ix_strava_activities_user_start", "user_id", "start_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Strava activity ids exceeded 2^31 years ago — BigInteger is required,
    # not defensive.
    strava_id: Mapped[int] = mapped_column(BigInteger, index=True)

    name: Mapped[str] = mapped_column(String(255))
    # `sport_type` is Strava's current, finer-grained field (e.g. "GravelRide");
    # `activity_type` is the legacy `type` ("Ride"), kept because older
    # activities and some third-party uploads only populate it.
    sport_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    activity_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # `start_date` is UTC. Strava also returns `start_date_local` plus a
    # timezone name and a UTC offset; we store the offset and the name rather
    # than a second timestamp column, because a "local" value in a
    # `DateTime(timezone=True)` column is a lie the database will happily
    # tell you forever. Reconstruct local time from these three.
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timezone_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    utc_offset_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    distance_m: Mapped[float] = mapped_column(Float, default=0.0)
    moving_time_s: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_time_s: Mapped[int] = mapped_column(Integer, default=0)
    total_elevation_gain_m: Mapped[float] = mapped_column(Float, default=0.0)

    average_speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_cadence: Mapped[float | None] = mapped_column(Float, nullable=True)

    average_heartrate: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_heartrate: Mapped[float | None] = mapped_column(Float, nullable=True)

    average_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    weighted_average_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    # False means the watts above are Strava's *estimate*, not a power meter.
    # Averaging the two together would be meaningless, so the flag has to survive.
    device_watts: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    kilojoules: Mapped[float | None] = mapped_column(Float, nullable=True)

    suffer_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    trainer: Mapped[bool] = mapped_column(Boolean, default=False)
    commute: Mapped[bool] = mapped_column(Boolean, default=False)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    private: Mapped[bool] = mapped_column(Boolean, default=False)

    gear_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    kudos_count: Mapped[int] = mapped_column(Integer, default=0)
    achievement_count: Mapped[int] = mapped_column(Integer, default=0)
    pr_count: Mapped[int] = mapped_column(Integer, default=0)

    start_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Google-encoded summary polyline. Stored so a future map view costs no
    # API calls; nothing reads it yet.
    map_polyline: Mapped[str | None] = mapped_column(Text, nullable=True)

    # source_id / source_ts / synced_at / content_hash inherited from
    # SourcedRecordMixin. `source_id` holds str(strava_id); `source_ts` holds
    # the activity start, so cross-source queries can order Strava alongside
    # other integrations' records without knowing this table's column names.
