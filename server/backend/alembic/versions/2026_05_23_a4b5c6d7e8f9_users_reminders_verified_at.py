"""users.reminders_verified_at — bridge-liveness for the reminders daemon

Nullable timestamp on `users`. The comar-client daemon stamps this on every
reminders-poll iteration (via POST /api/v1/reminders/verified), so the
server can distinguish "data unchanged in N hours" from "daemon died N
hours ago". Reminder.synced_at remains the data-change signal; this is
strictly liveness.

Safe to roll forward — additive nullable column, no defaults to backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("reminders_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "reminders_verified_at")
