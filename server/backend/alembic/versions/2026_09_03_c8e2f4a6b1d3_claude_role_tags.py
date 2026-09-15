"""The Claude flag becomes three role tags.

`#youdoit` said "an agent session should take this" without saying what kind
of taking: do the work, investigate and report, or fix the task line itself.
Alex asked for the split on 2026-09-02. Existing flags were all "do" in
intent — that is what /youdoit did with them — so they become `#claude/do`.

Revision ID: c8e2f4a6b1d3
Revises: b7d3e5f1a9c2
Create Date: 2026-09-03
"""

from alembic import op

revision = "c8e2f4a6b1d3"
down_revision = "b7d3e5f1a9c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE tasks SET tags = array_replace(tags, '#youdoit', '#claude/do') "
        "WHERE '#youdoit' = ANY(tags)"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE tasks SET tags = array_replace(tags, '#claude/do', '#youdoit') "
        "WHERE '#claude/do' = ANY(tags)"
    )
