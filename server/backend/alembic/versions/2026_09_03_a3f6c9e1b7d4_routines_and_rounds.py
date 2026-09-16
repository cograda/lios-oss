"""Routines and rounds — the recurring class of loop (chunk E3)

Vocabulary ruled by Alex 2026-09-03: a **loop** is anything with a
definition of done; a **routine** is a recurring loop template that never
closes itself; a **round** is one occurrence of a routine, and it closes.

A round IS a `tasks` row — `tasks.routine_id` — so it gets every existing
lens, queue, block, comment, history and transfer/accept semantic for free.
There is deliberately no separate occurrence table.

`task_domain_tags` gets a fourth target column (`routine_id`), following the
same many-to-many shape it already has for program/project/task — its CHECK
constraint (exactly one target set) is dropped and recreated over four
columns instead of three.

`task_events` gets a `routine_id` column: set on routine-level events (a
routine's own transfer request/accept/decline, where there is no single
round to attach to) and alongside `task_id` on a round's 'minted'/'skip'
events, so a routine's whole history is one query away.

Revision ID: a3f6c9e1b7d4
Revises: d1a4b7e9c2f6
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa

revision = "a3f6c9e1b7d4"
down_revision = "d1a4b7e9c2f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "routines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uid", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("done_when", sa.Text(), nullable=False),
        sa.Column("default_owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("pending_owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("transfer_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("program_id", sa.Integer(), sa.ForeignKey("task_programs.id"), nullable=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("task_projects.id"), nullable=True),
        sa.Column("schedule_kind", sa.String(10), nullable=False),
        sa.Column("schedule_spec", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("uid", name="uq_routines_uid"),
        sa.CheckConstraint(
            "schedule_kind IN ('fixed', 'interval', 'window')",
            name="ck_routines_schedule_kind",
        ),
    )
    op.create_index("ix_routines_uid", "routines", ["uid"])
    op.create_index("ix_routines_default_owner_id", "routines", ["default_owner_id"])
    op.create_index("ix_routines_pending_owner_id", "routines", ["pending_owner_id"])
    op.create_index("ix_routines_program_id", "routines", ["program_id"])
    op.create_index("ix_routines_project_id", "routines", ["project_id"])
    op.create_index("ix_routines_active", "routines", ["active"])

    op.create_table(
        "routine_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("routine_id", sa.Integer(), sa.ForeignKey("routines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.UniqueConstraint("routine_id", "ord", name="uq_routine_steps_routine_ord"),
    )
    op.create_index("ix_routine_steps_routine_id", "routine_steps", ["routine_id"])

    op.add_column("tasks", sa.Column("routine_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_tasks_routine_id", "tasks", "routines", ["routine_id"], ["id"])
    op.create_index("ix_tasks_routine_id", "tasks", ["routine_id"])

    op.add_column("task_events", sa.Column("routine_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_task_events_routine_id", "task_events", "routines", ["routine_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_task_events_routine_id", "task_events", ["routine_id"])

    op.add_column("task_domain_tags", sa.Column("routine_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_task_domain_tags_routine_id", "task_domain_tags", "routines", ["routine_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_task_domain_tags_routine_id", "task_domain_tags", ["routine_id"])
    op.execute("ALTER TABLE task_domain_tags DROP CONSTRAINT IF EXISTS ck_task_domain_tags_one_target")
    op.create_check_constraint(
        "ck_task_domain_tags_one_target", "task_domain_tags",
        "(program_id IS NOT NULL)::int + (project_id IS NOT NULL)::int "
        "+ (task_id IS NOT NULL)::int + (routine_id IS NOT NULL)::int = 1",
    )
    op.create_unique_constraint(
        "uq_task_domain_tags_routine", "task_domain_tags", ["domain_id", "routine_id"],
    )


def downgrade() -> None:
    op.execute("ALTER TABLE task_domain_tags DROP CONSTRAINT IF EXISTS uq_task_domain_tags_routine")
    op.execute("ALTER TABLE task_domain_tags DROP CONSTRAINT IF EXISTS ck_task_domain_tags_one_target")
    op.create_check_constraint(
        "ck_task_domain_tags_one_target", "task_domain_tags",
        "(program_id IS NOT NULL)::int + (project_id IS NOT NULL)::int "
        "+ (task_id IS NOT NULL)::int = 1",
    )
    op.execute("DROP INDEX IF EXISTS ix_task_domain_tags_routine_id")
    op.execute("ALTER TABLE task_domain_tags DROP CONSTRAINT IF EXISTS fk_task_domain_tags_routine_id")
    op.drop_column("task_domain_tags", "routine_id")

    op.execute("DROP INDEX IF EXISTS ix_task_events_routine_id")
    op.execute("ALTER TABLE task_events DROP CONSTRAINT IF EXISTS fk_task_events_routine_id")
    op.drop_column("task_events", "routine_id")

    op.execute("DROP INDEX IF EXISTS ix_tasks_routine_id")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS fk_tasks_routine_id")
    op.drop_column("tasks", "routine_id")

    op.execute("DROP INDEX IF EXISTS ix_routine_steps_routine_id")
    op.drop_table("routine_steps")

    op.execute("DROP INDEX IF EXISTS ix_routines_active")
    op.execute("DROP INDEX IF EXISTS ix_routines_project_id")
    op.execute("DROP INDEX IF EXISTS ix_routines_program_id")
    op.execute("DROP INDEX IF EXISTS ix_routines_pending_owner_id")
    op.execute("DROP INDEX IF EXISTS ix_routines_default_owner_id")
    op.execute("DROP INDEX IF EXISTS ix_routines_uid")
    op.drop_table("routines")
