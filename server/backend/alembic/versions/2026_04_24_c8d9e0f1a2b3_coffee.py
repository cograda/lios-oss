"""coffee integration

Adds coffee_equipment_profiles, coffees, and coffee_brews tables — ported
from brewhaha. Tracks coffee bags tasted, brew sessions (espresso + filter +
cafe), and equipment setups. flavour_notes stored inline as text[] on brews.
ai_suggestion is a JSONB blob populated by coffee_dial_in.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "coffee_equipment_profiles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("grinder_name", sa.String(200), nullable=True),
        sa.Column("grinder_type", sa.String(20), nullable=True),
        sa.Column("grinder_setting_label", sa.String(100), nullable=True),
        sa.Column("grinder_setting_range", sa.String(100), nullable=True),
        sa.Column("brewer_name", sa.String(200), nullable=True),
        sa.Column("brewer_type", sa.String(100), nullable=True),
        sa.Column("has_pid", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("has_pressure_gauge", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("portafilter_mm", sa.Integer, nullable=True),
        sa.Column("pressure_range", sa.String(100), nullable=True),
        sa.Column("default_dose_g", sa.Numeric(5, 1), nullable=True),
        sa.Column("default_yield_g", sa.Numeric(5, 1), nullable=True),
        sa.Column("default_water_g", sa.Numeric(6, 1), nullable=True),
        sa.Column("default_temp_c", sa.Numeric(4, 1), nullable=True),
        sa.Column("default_time_s", sa.Integer, nullable=True),
        sa.Column("default_pressure_bar", sa.Numeric(4, 1), nullable=True),
        sa.Column("is_default", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "coffees",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("roaster", sa.String(200), nullable=True),
        sa.Column("origin_country", sa.String(100), nullable=True),
        sa.Column("region_farm", sa.String(300), nullable=True),
        sa.Column("process", sa.String(100), nullable=True),
        sa.Column("fermentation", sa.String(200), nullable=True),
        sa.Column("variety", sa.String(200), nullable=True),
        sa.Column("altitude_masl", sa.String(50), nullable=True),
        sa.Column("roast_date", sa.Date, nullable=True),
        sa.Column("purchase_date", sa.Date, nullable=True),
        sa.Column("roaster_tasting_notes", sa.Text, nullable=True),
        sa.Column("category", sa.String(50), nullable=True),
        sa.Column("price", sa.Numeric(8, 2), nullable=True),
        sa.Column("weight_g", sa.Numeric(6, 1), nullable=True),
        sa.Column("status", sa.String(20), server_default="current", nullable=False),
        sa.Column("photo_url", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("rating", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", sa.String(500), nullable=True),
        sa.Column("source_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
    )
    op.create_index("ix_coffees_status", "coffees", ["status"])
    op.create_index("ix_coffees_roaster", "coffees", ["roaster"])
    op.create_index("ix_coffees_origin_country", "coffees", ["origin_country"])
    op.create_index("ix_coffees_source_id", "coffees", ["source_id"])
    op.create_unique_constraint(
        "uq_coffees_name_roaster", "coffees", ["name", "roaster"],
    )

    op.create_table(
        "coffee_brews",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "coffee_id", sa.Integer,
            sa.ForeignKey("coffees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "equipment_profile_id", sa.Integer,
            sa.ForeignKey("coffee_equipment_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("brew_context", sa.String(20), server_default="home", nullable=False),
        sa.Column("cafe_name", sa.String(200), nullable=True),
        sa.Column("drink_type", sa.String(100), nullable=True),
        sa.Column("brewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dose_g", sa.Numeric(5, 1), nullable=True),
        sa.Column("grind_setting", sa.String(100), nullable=True),
        sa.Column("water_temp_c", sa.Numeric(4, 1), nullable=True),
        sa.Column("yield_g", sa.Numeric(5, 1), nullable=True),
        sa.Column("time_s", sa.Integer, nullable=True),
        sa.Column("pressure_bar", sa.Numeric(4, 1), nullable=True),
        sa.Column("ratio", sa.Numeric(4, 2), nullable=True),
        sa.Column("water_g", sa.Numeric(6, 1), nullable=True),
        sa.Column("brew_time_s", sa.Integer, nullable=True),
        sa.Column("filter_ratio", sa.Numeric(5, 2), nullable=True),
        sa.Column("acidity", sa.SmallInteger, nullable=True),
        sa.Column("sweetness", sa.SmallInteger, nullable=True),
        sa.Column("body", sa.SmallInteger, nullable=True),
        sa.Column("bitterness", sa.SmallInteger, nullable=True),
        sa.Column("overall", sa.SmallInteger, nullable=True),
        sa.Column("flavour_notes", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("milk_drink", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("milk_type", sa.String(100), nullable=True),
        sa.Column("milk_temp_c", sa.Numeric(4, 1), nullable=True),
        sa.Column("extraction_assessment", sa.String(20), nullable=True),
        sa.Column("ai_suggestion", postgresql.JSONB, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_coffee_brews_brewed_at", "coffee_brews", ["brewed_at"])
    op.create_index("ix_coffee_brews_coffee_id", "coffee_brews", ["coffee_id"])
    op.create_index("ix_coffee_brews_method", "coffee_brews", ["method"])


def downgrade() -> None:
    op.drop_index("ix_coffee_brews_method", table_name="coffee_brews")
    op.drop_index("ix_coffee_brews_coffee_id", table_name="coffee_brews")
    op.drop_index("ix_coffee_brews_brewed_at", table_name="coffee_brews")
    op.drop_table("coffee_brews")
    op.drop_constraint("uq_coffees_name_roaster", "coffees", type_="unique")
    op.drop_index("ix_coffees_source_id", table_name="coffees")
    op.drop_index("ix_coffees_origin_country", table_name="coffees")
    op.drop_index("ix_coffees_roaster", table_name="coffees")
    op.drop_index("ix_coffees_status", table_name="coffees")
    op.drop_table("coffees")
    op.drop_table("coffee_equipment_profiles")
