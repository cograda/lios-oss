"""Multi-user loops: accept semantics on the task ledger

Transfer is a request, not a write ("TCP not UDP" — Alex, 2026-09-03).
`pending_owner_id` names who has been asked to take a task/project/program;
`owner_id` only moves once `tasks_accept` is called by that pending owner.
`transfer_requested_at` is when the request was made, so a stale ask (never
accepted or declined) is visible rather than silent.

Nudges are not a new column: they are `task_events` rows with
`field='nudge'`, counted at read time (see tools.py's `_row`), the same
pattern the ledger already uses for notes and status transitions.

Revision ID: d1a4b7e9c2f6
Revises: c8e2f4a6b1d3
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa

revision = "d1a4b7e9c2f6"
down_revision = "c8e2f4a6b1d3"
branch_labels = None
depends_on = None



# task_projects.owner_id carries no index in the ORM either (unlike Task's
# and TaskProgram's) — mirrored exactly here so `compare_metadata` sees no
# drift between the migrated schema and the models.
_INDEXED = ("tasks", "task_programs")


def upgrade() -> None:
    for table in ("tasks", "task_projects", "task_programs"):
        op.add_column(table, sa.Column("pending_owner_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_pending_owner_id", table, "users",
            ["pending_owner_id"], ["id"],
        )
        op.add_column(table, sa.Column("transfer_requested_at", sa.DateTime(timezone=True), nullable=True))
        if table in _INDEXED:
            op.create_index(f"ix_{table}_pending_owner_id", table, ["pending_owner_id"])


def downgrade() -> None:
    for table in ("tasks", "task_projects", "task_programs"):
        if table in _INDEXED:
            op.execute(f"DROP INDEX IF EXISTS ix_{table}_pending_owner_id")
        op.drop_column(table, "transfer_requested_at")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_pending_owner_id")
        op.drop_column(table, "pending_owner_id")
