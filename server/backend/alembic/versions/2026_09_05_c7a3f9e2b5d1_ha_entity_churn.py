"""ha entity churn log — removals leave a trace (Wave 5.9).

Adds `ha_entity_churn`, an append-only log of Home Assistant entities
appearing ("added") or disappearing ("removed") from `/api/states`.
`ha_entities` is upserted in place and a vanished entity is hard-deleted on
reconcile (`homeassistant/sync.py`) — `first_seen_at` (migration
`d5e2b8f1a9c4`, 2026-08-19) covers only the "appeared" half, so a
disappearance still left no trace at all: "HA went 1,573 -> 1,717 entities
in 24h" was unrecoverable from stored data, because the only surviving
artefact was a count.

`sync.py` now writes one `ha_entity_churn` row before each such delete
(snapshotting `domain`/`friendly_name`/`last_state` off the ORM row before
it is gone) and one on first sight of a new entity. Household-shared, no
`UserOwnedMixin` — same reasoning as `ha_entities`/`ha_state_changes`: HA is
a house signal, not a per-user one.

Revision ID: c7a3f9e2b5d1
Revises: 861756c2cf64
Create Date: 2026-09-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7a3f9e2b5d1"
down_revision: Union[str, Sequence[str], None] = "861756c2cf64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ha_entity_churn",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("event", sa.String(length=16), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=True),
        sa.Column("friendly_name", sa.String(length=255), nullable=True),
        sa.Column("last_state", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_ha_entity_churn_entity_id", "ha_entity_churn", ["entity_id"])
    op.create_index("ix_ha_entity_churn_event", "ha_entity_churn", ["event"])
    op.create_index("ix_ha_entity_churn_at", "ha_entity_churn", ["at"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ha_entity_churn")
