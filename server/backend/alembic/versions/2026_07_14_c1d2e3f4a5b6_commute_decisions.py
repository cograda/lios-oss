"""commute decisions

commute_decisions holds one row per solver run (weekday mornings, roughly
once a minute in the 07:00-08:59 window) — the leave-by instruction, target
bus/train, and the Howth interchange delay used to tune howth_buffer_min.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "commute_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("status_text", sa.Text(), nullable=False),
        sa.Column("leave_in_min", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("target_bus_trip_id", sa.String(length=128), nullable=True),
        sa.Column("target_bus_route", sa.String(length=8), nullable=True),
        sa.Column("target_bus_dep_home", sa.DateTime(), nullable=True),
        sa.Column("target_bus_arr_howth", sa.DateTime(), nullable=True),
        sa.Column("target_train_code", sa.String(length=16), nullable=True),
        sa.Column("target_train_howth_dep", sa.DateTime(), nullable=True),
        sa.Column("target_train_central_arr", sa.DateTime(), nullable=True),
        sa.Column("howth_delay_min", sa.Float(), nullable=True),
        sa.Column("bus_feed_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dart_feed_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bus_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("dart_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ha_pushed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_commute_decisions_decided_at", "commute_decisions", ["decided_at"])
    op.create_index("ix_commute_decisions_state", "commute_decisions", ["state"])


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_commute_decisions_state"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_commute_decisions_decided_at"))
    op.execute(sa.text("DROP TABLE IF EXISTS commute_decisions"))
