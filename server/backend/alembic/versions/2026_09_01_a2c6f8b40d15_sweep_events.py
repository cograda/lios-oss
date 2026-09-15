"""task_events.task_id nullable, so a sweep can be recorded

Revision ID: a2c6f8b40d15
Revises: f4b8d2e71c39
Create Date: 2026-08-31

The design of record says `last-reviewed` "is a property of a *sweep*, not of
a file" and belongs in task_events as a sweep record. The table as built made
task_id NOT NULL, so that record could not exist -- a sweep is a review of the
whole backlog and belongs to no single task.

Nullable task_id, therefore, and a sweep is `to_status='swept'` with no task.
The rendered file's `last-reviewed` is then COMPUTED from the newest one
rather than stamped by whatever last wrote the file, which is what stops a
regeneration from silently claiming somebody read the backlog.
"""
from alembic import op
import sqlalchemy as sa

revision = "a2c6f8b40d15"
down_revision = "f4b8d2e71c39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("task_events", "task_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM task_events WHERE task_id IS NULL")
    op.alter_column("task_events", "task_id", existing_type=sa.Integer(), nullable=False)
