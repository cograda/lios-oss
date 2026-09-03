"""tasks.import_key — recognise a row the import already created

Revision ID: d8f2b6c31a47
Revises: c7e1a4d92f60
Create Date: 2026-08-31

Not in the design of record's schema, and needed the moment an import exists:
without it, re-running the import duplicates every row. It is a content hash
over the normalised task text — deliberately NOT the task's identity, which is
`uid` (TASK-0042) and must survive an edit to the wording.

⚠️ Editing a task's title in the source file therefore yields a new key, so a
re-import would see an unmatched row rather than an update. That is the right
trade while the file is still the input: noticing an unmatched row is
recoverable, silently rewriting the wrong task is not. Once the render goes
one-way the file stops being an input at all and this column becomes history.
"""
from alembic import op
import sqlalchemy as sa

revision = "d8f2b6c31a47"
down_revision = "c7e1a4d92f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("import_key", sa.String(length=20), nullable=True))
    op.create_index("ix_tasks_import_key", "tasks", ["import_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tasks_import_key", table_name="tasks")
    op.drop_column("tasks", "import_key")
