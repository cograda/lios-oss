"""health_workouts.source / .notes — manual workout entry (issue #185).

Adds two nullable columns so `health_workout_add` (the new manual-entry MCP
tool) has somewhere to put a caller-supplied source label and free-text
notes/sets. Both are `NULL` for every pre-existing row (Apple Health push
sync never sets them), and the read handlers in `tools.py` only emit
`"source"`/`"notes"` in their JSON output when the column is actually
populated — a synced HealthKit workout renders identically to before this
migration, so `tests/snapshots/health_workouts.json` needed no update.

`source` is a label, not a foreign key into another sync's identity scheme
(Strava activities stay in their own `strava_activities` table — see the
docstring on `#195`'s brief-merge change for why that stays a separate
source rather than being folded into this table). Manual rows key off
`uid = "manual:<uuid4>"`; nothing here changes the existing
`uq_health_workout_user_uid` constraint.

Revision ID: 6548357098fa
Revises: a1c4d7e2f5b9
Create Date: 2026-09-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6548357098fa"
down_revision: Union[str, Sequence[str], None] = "a1c4d7e2f5b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "health_workouts",
        sa.Column("source", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "health_workouts",
        sa.Column("notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # IF EXISTS: a downgrade run against a database where an earlier partial
    # upgrade left one column but not the other must not itself fail.
    op.execute("ALTER TABLE health_workouts DROP COLUMN IF EXISTS notes")
    op.execute("ALTER TABLE health_workouts DROP COLUMN IF EXISTS source")
