"""ha_entities.last_updated — distinguish "value never changes" from "heard nothing" (issue #167).

`ha_entities.last_changed` only moves when an entity's state *value*
changes; HA's own `last_updated` moves on every state write it receives,
attribute-only changes included. A publisher that has died leaves both
frozen at the same instant — which is exactly what happened to
`sensor.shed_cam_temperature` from 2026-08-30, read by `host_fleet.py`'s
`ha_entity:` probe as a clean `ok` because the value was a number and not
`unavailable`. Recording `last_updated` lets a staleness check ask "has HA
heard anything from this entity recently" instead of "did its value ever
change".

Nullable: rows written before this migration (and any write path not yet
touching the column) have none, and callers must treat that as "can't
judge freshness" rather than as evidence of staleness.

Revision ID: a1c4d7e2f5b9
Revises: f1c8a4d6e9b2
Create Date: 2026-09-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1c4d7e2f5b9"
down_revision: Union[str, Sequence[str], None] = "f1c8a4d6e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ha_entities",
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ha_entities", "last_updated")
