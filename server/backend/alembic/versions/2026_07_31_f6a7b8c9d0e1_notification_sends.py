"""notification_sends — send ledger for the notifications integration

Creates one new table, owned entirely by the new `notifications` package. No
existing table is touched, so there is nothing here that can conflict with prod
state; the only non-obvious bit is the partial unique index.

`ix_notification_sends_open_fingerprint` is UNIQUE ... WHERE resolved_at IS
NULL — "at most one *open* alert per fingerprint", while allowing unlimited
resolved history for the same fingerprint (a problem that recurs gets a fresh
row). A plain unique constraint cannot express that, which is why this is a raw
`op.create_index(..., postgresql_where=...)` rather than a constraint.

Per the repo's migration convention, DDL is written defensively (`IF EXISTS` on
the way down) so a re-run tolerates either pre-existing shape.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_sends",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="warning"),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Non-unique: lookups by fingerprint span resolved history too.
    op.create_index(
        "ix_notification_sends_fingerprint", "notification_sends", ["fingerprint"],
    )
    op.create_index(
        "ix_notification_sends_resolved_at", "notification_sends", ["resolved_at"],
    )
    # The dedup invariant — see module docstring.
    op.create_index(
        "ix_notification_sends_open_fingerprint",
        "notification_sends",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_notification_sends_open_fingerprint")
    op.execute("DROP INDEX IF EXISTS ix_notification_sends_resolved_at")
    op.execute("DROP INDEX IF EXISTS ix_notification_sends_fingerprint")
    op.execute("DROP TABLE IF EXISTS notification_sends")
