"""reminder_commands.claimed_at — recover a stranded `draining` row.

`drain_pending`'s `_claim()` moves a row `pending` -> `draining` before
dispatching it, so two concurrent drains (two daemons for one user) can't
both replay the same command. That left a gap: if the process dies between
the claim and the dispatch — a deploy restart, an OOM kill, one of the
several-times-a-day container recreates — nothing ever revisits a `draining`
row. `expire_stale` and `_fetch_candidates` both filter on `status ==
"pending"`, so the row is invisible to every future drain, and
`pending_writes()` (the axis-6 dead-subscription alert) was the same —
exactly the write this whole channel exists to protect becomes permanently
silent, which is worse than the bug the channel was built to fix.

`claimed_at` records when a claim was taken, so `drain_pending` can reset
any `draining` row older than a short window (`DRAINING_CLAIM_TIMEOUT`,
commands.py) back to `pending` before selecting candidates — recovering it
for the very next drain rather than leaving it orphaned until `MAX_REPLAY_AGE`
quietly expires it as a lost write it never actually needed to be.

Revision ID: f2b6d8a1c4e7
Revises: e8c1a5f9d3b2
Create Date: 2026-09-04
"""

from alembic import op
import sqlalchemy as sa

revision = "f2b6d8a1c4e7"
down_revision = "e8c1a5f9d3b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: safe to re-run against a database where a previous
    # attempt half-applied.
    op.execute(
        "ALTER TABLE reminder_commands "
        "ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE reminder_commands DROP COLUMN IF EXISTS claimed_at")
