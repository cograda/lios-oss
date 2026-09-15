"""tasks.kind + tasks.severity — the ledger IS the register of bugs and features

Revision ID: 99022513af93
Revises: b7d2e9f4a1c3
Create Date: 2026-09-07

Decision S5.3 (Alex, 2026-09-07): lios defects and feature asks are loops
like any other — a definition of done, an owner, a project, history — so they
are `tasks` rows with a `kind`, not a second table with a second set of
lenses. `kind` is NOT NULL with a server default of 'task', so every existing
row reads as an ordinary task with no backfill; `severity` is nullable and
applies to bugs and features.

Both constraints are dropped IF EXISTS before being created, so the migration
tolerates a half-applied earlier attempt (the repo's convention — see the
2026-05-02 users migration for the crash loop that taught it).
"""

from alembic import op
import sqlalchemy as sa

revision = "99022513af93"
down_revision = "b7d2e9f4a1c3"
branch_labels = None
depends_on = None

KINDS = ("task", "bug", "feature", "chore")
SEVERITIES = ("critical", "high", "medium", "low")


def upgrade() -> None:
    op.add_column("tasks", sa.Column(
        "kind", sa.String(10), nullable=False, server_default="task",
    ))
    op.add_column("tasks", sa.Column("severity", sa.String(10), nullable=True))
    op.execute("DROP INDEX IF EXISTS ix_tasks_kind")
    op.create_index("ix_tasks_kind", "tasks", ["kind"])
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_kind")
    op.create_check_constraint("ck_tasks_kind", "tasks", f"kind IN {KINDS!r}")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_severity")
    op.create_check_constraint(
        "ck_tasks_severity", "tasks", f"severity IS NULL OR severity IN {SEVERITIES!r}",
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_severity")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_kind")
    op.execute("DROP INDEX IF EXISTS ix_tasks_kind")
    op.drop_column("tasks", "severity")
    op.drop_column("tasks", "kind")
