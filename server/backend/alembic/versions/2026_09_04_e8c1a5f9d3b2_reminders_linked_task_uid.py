"""reminders.linked_task_uid — the ledger inlet (E chunk 6b)

Apple Reminders becomes an inlet only: a reminder not yet linked to a ledger
task becomes one, a task done in the ledger completes its reminder, and a
reminder completed on the phone completes its linked task. This column is
the durable link between the two, in both directions, for the periodic
inlet tick (`app/integrations/apple_reminders/inlet.py`) to read and write.

A plain string holding `tasks.uid` (e.g. "TASK-0042"), not a foreign key —
apple_reminders may not import `tasks.models` across the capability
boundary, and a `tasks.uid` string is this codebase's existing convention
for a cross-package reference (see `TaskLink.target_ref`). Retires
`/reconcile-reminders`.

Revision ID: e8c1a5f9d3b2
Revises: a3f6c9e1b7d4
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa

revision = "e8c1a5f9d3b2"
down_revision = "a3f6c9e1b7d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("linked_task_uid", sa.String(length=20), nullable=True),
    )
    op.create_index(
        "ix_reminders_linked_task_uid", "reminders", ["linked_task_uid"],
    )


def downgrade() -> None:
    # IF EXISTS throughout — a partially-applied upgrade must still reverse.
    op.execute("DROP INDEX IF EXISTS ix_reminders_linked_task_uid")
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS linked_task_uid")
