"""home assistant entities + state change history

ha_entities holds the latest state per HA entity (upserted by the 5-min
sync); ha_state_changes is the append-only non-numeric transition history
that answers "when did the dishwasher last run".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a6b7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ha_entities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("friendly_name", sa.String(length=255), nullable=True),
        sa.Column("area", sa.String(length=128), nullable=True),
        sa.Column("device_class", sa.String(length=64), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("attributes", JSONB(), nullable=True),
        sa.Column("last_changed", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_ha_entities_entity_id", "ha_entities", ["entity_id"], unique=True
    )
    op.create_index("ix_ha_entities_domain", "ha_entities", ["domain"])

    op.create_table(
        "ha_state_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("old_state", sa.Text(), nullable=True),
        sa.Column("new_state", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attributes", JSONB(), nullable=True),
    )
    op.create_index(
        "ix_ha_state_changes_entity_id", "ha_state_changes", ["entity_id"]
    )
    op.create_index(
        "ix_ha_state_changes_changed_at", "ha_state_changes", ["changed_at"]
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS ha_state_changes"))
    op.execute(sa.text("DROP TABLE IF EXISTS ha_entities"))
