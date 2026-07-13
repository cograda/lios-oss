"""client_task_health: per-device daemon task-health snapshot

The daemon's supervised loops (ISS-001 fix) report per-task liveness with
each heartbeat; storing it on the device's token row makes a stalled loop
visible server-side instead of surfacing as mysteriously stale reminders.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("client_tokens", sa.Column("task_health", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("client_tokens", "task_health")
