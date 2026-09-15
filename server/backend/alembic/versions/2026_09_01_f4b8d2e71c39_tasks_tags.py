"""tasks.tags — the file's tags, which nothing was storing

Revision ID: f4b8d2e71c39
Revises: e3a7c19d5b82
Create Date: 2026-08-31

Found by rendering the imported backlog back out and diffing it against the
original: every tag that was not #errand/#sitdown/#quick/#deep disappeared.
Those four became columns (context, energy); the rest -- #home-automation,
#focus, #person/isla, #schedule, #movein and the domain labels -- were parsed,
used to pick a column, and then dropped on the floor.

text[] follows `domains.checklist`, which is the existing precedent for a
small list of strings in this database.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f4b8d2e71c39"
down_revision = "e3a7c19d5b82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column(
        "tags", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}",
    ))


def downgrade() -> None:
    op.drop_column("tasks", "tags")
