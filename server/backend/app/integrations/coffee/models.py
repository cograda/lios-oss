"""SQLAlchemy models for coffee integration — ported from brewhaha."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin, UserOwnedMixin


class CoffeeEquipmentProfile(Base):
    """Grinder + brewer setup. Drives dial-in suggestions."""

    __tablename__ = "coffee_equipment_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(20))  # 'espresso' | 'filter'

    grinder_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    grinder_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 'stepless' | 'stepped'
    grinder_setting_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    grinder_setting_range: Mapped[str | None] = mapped_column(String(100), nullable=True)

    brewer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    brewer_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    has_pid: Mapped[bool] = mapped_column(Boolean, default=False)
    has_pressure_gauge: Mapped[bool] = mapped_column(Boolean, default=False)
    portafilter_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pressure_range: Mapped[str | None] = mapped_column(String(100), nullable=True)

    default_dose_g: Mapped[Decimal | None] = mapped_column(Numeric(5, 1), nullable=True)
    default_yield_g: Mapped[Decimal | None] = mapped_column(Numeric(5, 1), nullable=True)
    default_water_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 1), nullable=True)
    default_temp_c: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    default_time_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_pressure_bar: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)

    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<CoffeeEquipmentProfile {self.name} ({self.method})>"


class Coffee(SourcedRecordMixin, Base):
    """One row per coffee bag tasted."""

    __tablename__ = "coffees"
    __table_args__ = (
        UniqueConstraint("name", "roaster", name="uq_coffees_name_roaster"),
        Index("ix_coffees_status", "status"),
        Index("ix_coffees_roaster", "roaster"),
        Index("ix_coffees_origin_country", "origin_country"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    roaster: Mapped[str | None] = mapped_column(String(200), nullable=True)
    origin_country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region_farm: Mapped[str | None] = mapped_column(String(300), nullable=True)
    process: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fermentation: Mapped[str | None] = mapped_column(String(200), nullable=True)
    variety: Mapped[str | None] = mapped_column(String(200), nullable=True)
    altitude_masl: Mapped[str | None] = mapped_column(String(50), nullable=True)
    roast_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    roaster_tasting_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)  # roast level
    price: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    weight_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 1), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="current")  # current|finished|freezer|incoming|wishlist
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-10
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # source_id, source_ts, synced_at, content_hash inherited from SourcedRecordMixin

    def __repr__(self) -> str:
        return f"<Coffee {self.name} ({self.roaster})>"


class CoffeeBrew(UserOwnedMixin, Base):
    """One row per brew session.

    Per-user: brews + ratings are personal. Beans (`coffees` table) and
    equipment profiles stay shared — one bag of beans is one physical object
    but each person's brew + rating is their own experience.
    """

    __tablename__ = "coffee_brews"
    __table_args__ = (
        Index("ix_coffee_brews_user_brewed_at", "user_id", "brewed_at"),
        Index("ix_coffee_brews_coffee_id", "coffee_id"),
        Index("ix_coffee_brews_method", "method"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    coffee_id: Mapped[int | None] = mapped_column(
        ForeignKey("coffees.id", ondelete="SET NULL"), nullable=True,
    )
    equipment_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("coffee_equipment_profiles.id", ondelete="SET NULL"), nullable=True,
    )
    method: Mapped[str] = mapped_column(String(20))  # 'espresso' | 'filter'
    brew_context: Mapped[str] = mapped_column(String(20), default="home")  # 'home' | 'cafe'
    cafe_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    drink_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    brewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    dose_g: Mapped[Decimal | None] = mapped_column(Numeric(5, 1), nullable=True)
    grind_setting: Mapped[str | None] = mapped_column(String(100), nullable=True)
    water_temp_c: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)

    # Espresso
    yield_g: Mapped[Decimal | None] = mapped_column(Numeric(5, 1), nullable=True)
    time_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pressure_bar: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    ratio: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)

    # Filter
    water_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 1), nullable=True)
    brew_time_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filter_ratio: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    # Tasting (1-5)
    acidity: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    sweetness: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    body: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    bitterness: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    overall: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    flavour_notes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    milk_drink: Mapped[bool] = mapped_column(Boolean, default=False)
    milk_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    milk_temp_c: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)

    extraction_assessment: Mapped[str | None] = mapped_column(String(20), nullable=True)  # under|good|over
    ai_suggestion: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<CoffeeBrew #{self.id} coffee={self.coffee_id} method={self.method}>"
