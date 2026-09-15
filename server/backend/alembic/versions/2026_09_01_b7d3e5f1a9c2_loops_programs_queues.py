"""loops: programs, domain tags (many-to-many), queues, containment, field history

Revision ID: b7d3e5f1a9c2
Revises: a2c6f8b40d15
Create Date: 2026-09-01

Decided with Alex on 2026-09-01, reading the work `taskdb` export against the
shipped ledger:

- the hierarchy is domains -> programs -> projects -> tasks, so `task_programs`
  is the new level and `task_projects.program_id` points at it;
- domain <-> program/project/task is MANY-TO-MANY (`task_domain_tags`), not a
  single value -- a tag, because real work is cross-cutting;
- explicit `week`/`focus` queues on tasks, separate from priority;
- field-level history on `task_events`.

Nothing is dropped. `tasks.domain_id` stays (it is NULL everywhere in
production -- `domains` has zero rows) and is superseded by the tag table.
"""
from alembic import op
import sqlalchemy as sa

revision = "b7d3e5f1a9c2"
down_revision = "a2c6f8b40d15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_programs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uid", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("note_path", sa.String(500), nullable=True),
        sa.Column("done_when", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("uid", name="uq_task_programs_uid"),
        sa.UniqueConstraint("title", name="uq_task_programs_title"),
    )
    op.create_index("ix_task_programs_uid", "task_programs", ["uid"])
    op.create_index("ix_task_programs_status", "task_programs", ["status"])
    op.create_index("ix_task_programs_owner_id", "task_programs", ["owner_id"])

    op.add_column("task_projects", sa.Column("program_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_task_projects_program_id", "task_projects", "task_programs",
        ["program_id"], ["id"],
    )
    op.create_index("ix_task_projects_program_id", "task_projects", ["program_id"])

    op.create_table(
        "task_domain_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain_id", sa.Integer(), sa.ForeignKey("domains.id", ondelete="CASCADE"), nullable=False),
        sa.Column("program_id", sa.Integer(), sa.ForeignKey("task_programs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("task_projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True),
        sa.CheckConstraint(
            "(program_id IS NOT NULL)::int + (project_id IS NOT NULL)::int "
            "+ (task_id IS NOT NULL)::int = 1",
            name="ck_task_domain_tags_one_target",
        ),
        sa.UniqueConstraint("domain_id", "program_id", name="uq_task_domain_tags_program"),
        sa.UniqueConstraint("domain_id", "project_id", name="uq_task_domain_tags_project"),
        sa.UniqueConstraint("domain_id", "task_id", name="uq_task_domain_tags_task"),
    )
    for col in ("domain_id", "program_id", "project_id", "task_id"):
        op.create_index(f"ix_task_domain_tags_{col}", "task_domain_tags", [col])

    op.add_column("tasks", sa.Column("queue", sa.String(10), nullable=True))
    op.add_column("tasks", sa.Column("queue_set_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tasks_queue", "tasks", ["queue"])

    op.add_column("task_events", sa.Column("field", sa.String(40), nullable=True))
    op.add_column("task_events", sa.Column("old_value", sa.Text(), nullable=True))
    op.add_column("task_events", sa.Column("new_value", sa.Text(), nullable=True))
    op.create_index("ix_task_events_field", "task_events", ["field"])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_task_events_field")
    op.drop_column("task_events", "new_value")
    op.drop_column("task_events", "old_value")
    op.drop_column("task_events", "field")
    op.execute("DROP INDEX IF EXISTS ix_tasks_queue")
    op.drop_column("tasks", "queue_set_at")
    op.drop_column("tasks", "queue")
    op.drop_table("task_domain_tags")
    op.execute("DROP INDEX IF EXISTS ix_task_projects_program_id")
    op.execute("ALTER TABLE task_projects DROP CONSTRAINT IF EXISTS fk_task_projects_program_id")
    op.drop_column("task_projects", "program_id")
    op.drop_table("task_programs")
