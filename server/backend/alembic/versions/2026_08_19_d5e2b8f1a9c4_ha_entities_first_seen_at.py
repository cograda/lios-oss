"""ha_entities.first_seen_at — make entity churn attributable

Home Assistant went from 1,573 to 1,717 entities in 24 hours (+46 offline) and
the change was **unattributable from stored data**: `ha_entities.synced_at` is
bumped on every sync (`sync.py`: `row.synced_at = now`), so it is a heartbeat,
not a first-seen. The only surviving artefact was a count, and a count cannot
tell you what changed.

`first_seen_at` is written on insert and never updated, so the next delta is
answerable by query rather than by forensics. Existing rows are backfilled to
`synced_at` — wrong in detail (it is "when we last saw it", not "when we first
did") but monotonically sane and strictly better than NULL, and the distinction
stops mattering after one sync cycle of new arrivals.

⚠️ Only half the gap is closed here. Entities removed from HA are still
hard-deleted by the sync loop, so a *disappearance* leaves no trace at all. A
churn log is the fix for that and is deliberately out of scope.

Revision ID: d5e2b8f1a9c4
Revises: c4f1a9d7e2b8
Create Date: 2026-08-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e2b8f1a9c4"
down_revision: Union[str, Sequence[str], None] = "c4f1a9d7e2b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable first so the ALTER never rewrites the table under a lock while
    # the app is live, then backfilled, then made NOT NULL.
    op.add_column(
        "ha_entities",
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE ha_entities SET first_seen_at = COALESCE(synced_at, now()) "
        "WHERE first_seen_at IS NULL"
    )
    op.alter_column(
        "ha_entities",
        "first_seen_at",
        nullable=False,
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    # IF EXISTS: a downgrade run against a database that never got the column
    # (or got it via create_tables rather than this migration) must not fail.
    op.execute("ALTER TABLE ha_entities DROP COLUMN IF EXISTS first_seen_at")
