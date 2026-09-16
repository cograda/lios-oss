"""runs — one structured row per scheduled job / tool call / manual run (S5.1).

`vault/Projects/lios/Backlog.md`: "One structured run record for every
scheduled job and tool call ... system_alerts answers 'what ran in the last
hour' from the ledger, not logs." Scheduled jobs (APScheduler, `app/
scheduler.py`) had no run record at all before this. `tool_calls` already
covers per-tool-call dispatch and is intentionally left as-is here — see
`app/models/runs.py`'s docstring for the planned merge and why the two
stay separate for now.

Kernel-owned table (`app/models/__init__.py::_KERNEL_OWNED`), nullable
`user_id` on the `NULLABLE_OR_ADMIN_USER_ID` allowlist (`app/privacy.py`) —
same rationale as `tool_calls`/`auth_events`: an admin/ops audit log, not
per-user application data.

Revision ID: b4d8f2a6c1e9
Revises: d91611c7baac
Create Date: 2026-09-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b4d8f2a6c1e9"
down_revision: Union[str, Sequence[str], None] = "d91611c7baac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("touched", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("trigger", sa.String(length=10), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_runs_run_id", "runs", ["run_id"])
    op.create_index("ix_runs_kind", "runs", ["kind"])
    op.create_index("ix_runs_name", "runs", ["name"])
    op.create_index("ix_runs_user_id", "runs", ["user_id"])
    op.create_index("ix_runs_outcome", "runs", ["outcome"])
    op.create_index("ix_runs_started_at", "runs", ["started_at"])
    op.create_index("ix_runs_name_started_at", "runs", ["name", "started_at"])


def downgrade() -> None:
    # IF EXISTS throughout — a partially-applied upgrade must still be
    # reversible (same rule as every other migration in this package).
    for index in (
        "ix_runs_name_started_at",
        "ix_runs_started_at",
        "ix_runs_outcome",
        "ix_runs_user_id",
        "ix_runs_name",
        "ix_runs_kind",
        "ix_runs_run_id",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index}")
    op.execute("DROP TABLE IF EXISTS runs")
