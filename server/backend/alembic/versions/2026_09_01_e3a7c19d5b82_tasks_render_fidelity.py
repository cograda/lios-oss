"""tasks: the three things a faithful render needs and the schema dropped

Revision ID: e3a7c19d5b82
Revises: d8f2b6c31a47
Create Date: 2026-08-31

Found by trying to render Task Backlog.md back out of the ledger and diffing
it against the original. The design of record modelled the file as a task
list; it is a task list *plus* editorial structure, and a render that dropped
that structure would delete it from the only copy.

  * `sort_order` (tasks and projects) — the file's order is information. It is
    roughly priority-descending but not exactly, and it is the order a person
    reads. Rendering by (priority, uid) instead would reshuffle all 246 items
    into a diff nobody could review.
  * `subsection` — the PTA section has THREE H3 headings ("Next 7 days",
    "Standing up the committee", "Fundraising and events"). Hierarchy was
    thought to be one level (H2 only); it is two wherever a project needs it.
  * `body_note` — five blockquote lines and two paragraphs sit under section
    headings, carrying context that is not any single task's ("Move-in tasks
    now live on the Comar Project Board", the PTA co-chair commitment).

None of these are new features. They are the file's existing content, and
they are here so the render can be lossless before it is made one-way.
"""
from alembic import op
import sqlalchemy as sa

revision = "e3a7c19d5b82"
down_revision = "d8f2b6c31a47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("subsection", sa.String(length=200), nullable=True))
    op.add_column("tasks", sa.Column("sort_order", sa.Integer(), nullable=True))
    op.add_column("task_projects", sa.Column("body_note", sa.Text(), nullable=True))
    op.add_column("task_projects", sa.Column("sort_order", sa.Integer(), nullable=True))
    op.create_index("ix_tasks_sort_order", "tasks", ["sort_order"])


def downgrade() -> None:
    op.drop_index("ix_tasks_sort_order", table_name="tasks")
    op.drop_column("task_projects", "sort_order")
    op.drop_column("task_projects", "body_note")
    op.drop_column("tasks", "sort_order")
    op.drop_column("tasks", "subsection")
