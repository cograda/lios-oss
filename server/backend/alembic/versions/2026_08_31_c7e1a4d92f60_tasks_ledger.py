"""tasks ledger: projects, tasks, links, comments, events

Revision ID: c7e1a4d92f60
Revises: 487b2290321a
Create Date: 2026-08-31

The task ledger's first migration. Two absences are deliberate and are
explained in `app/integrations/tasks/models.py`:

  * `tasks.created_at` is NULLABLE, because for the 239 items imported from
    Task Backlog.md the creation date is unrecoverable — the vault is
    gitignored and Drive-synced, and only 28% of items carry any date.
    Defaulting it to now() would manufacture a fact and silently corrupt
    every aging metric built on it afterwards.
  * there is no `tasks.parent_id`. The live file contains zero indented
    sub-tasks, so the column would ship with nothing to validate it. Adding
    it later is one nullable column.
"""
from alembic import op
import sqlalchemy as sa

revision = "c7e1a4d92f60"
down_revision = "487b2290321a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uid", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("domain_id", sa.Integer(), sa.ForeignKey("domains.id"), nullable=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("note_path", sa.String(length=500), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_task_projects_uid", "task_projects", ["uid"], unique=True)
    op.create_index("ix_task_projects_status", "task_projects", ["status"])
    op.create_index("ix_task_projects_domain_id", "task_projects", ["domain_id"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uid", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="inbox", nullable=False),
        sa.Column("priority", sa.String(length=10), nullable=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("task_projects.id"), nullable=True),
        sa.Column("domain_id", sa.Integer(), sa.ForeignKey("domains.id"), nullable=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("context", sa.String(length=30), nullable=True),
        sa.Column("energy", sa.String(length=10), nullable=True),
        sa.Column("estimate_min", sa.Integer(), nullable=True),
        sa.Column("defer_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=True),
        # Nullable on purpose — see the module docstring.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tasks_uid", "tasks", ["uid"], unique=True)
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_priority", "tasks", ["priority"])
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.create_index("ix_tasks_domain_id", "tasks", ["domain_id"])
    op.create_index("ix_tasks_owner_id", "tasks", ["owner_id"])
    op.create_index("ix_tasks_context", "tasks", ["context"])
    op.create_index("ix_tasks_defer_until", "tasks", ["defer_until"])
    op.create_index("ix_tasks_due_at", "tasks", ["due_at"])
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_index("ix_tasks_status_priority", "tasks", ["status", "priority"])
    op.create_index("ix_tasks_domain_status", "tasks", ["domain_id", "status"])

    op.create_table(
        "task_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("from_task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_ref", sa.String(length=500), nullable=False),
        sa.Column("predicate", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("derived_by", sa.String(length=30), server_default="human", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_links_from_task_id", "task_links", ["from_task_id"])
    op.create_index("ix_task_links_target_ref", "task_links", ["target_ref"])
    op.create_index("ix_task_links_from_predicate", "task_links", ["from_task_id", "predicate"])

    op.create_table(
        "task_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_comments_task_id", "task_comments", ["task_id"])

    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_index("ix_task_events_at", "task_events", ["at"])
    op.create_index("ix_task_events_task_at", "task_events", ["task_id", "at"])


def downgrade() -> None:
    op.drop_table("task_events")
    op.drop_table("task_comments")
    op.drop_table("task_links")
    op.drop_table("tasks")
    op.drop_table("task_projects")
