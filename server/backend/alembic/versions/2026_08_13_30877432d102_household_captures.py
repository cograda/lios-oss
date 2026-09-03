"""household capture inbox — Phase A2/A4 of household-ops-and-loops-2026-08.md

Hand-written (no local Postgres/Docker available in this environment to run
`alembic revision --autogenerate`), same as the preceding `domains` migration
this chains after. Mirrors `app/integrations/household/models.py`'s
`HouseholdCapture` (per-sender, UserOwnedMixin) and
`HouseholdCaptureSourceMessage` (idempotency ledger, scoped the same way
`snag_source_messages` is — `(user_id, message_ref)` unique, where user_id
here is the BRIDGE ingestion user, not the attributed sender).

No seed rows — captures are created at runtime via `household_capture_add`
or `household_capture_capture`, never a migration default
(tests/test_personalisation_guard.py).

`DROP ... IF EXISTS` throughout `downgrade()` per user-memory
`feedback_alembic_migration_safety.md`.

Revision ID: 30877432d102
Revises: aab4759a66ce
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "30877432d102"
down_revision: Union[str, Sequence[str], None] = "aab4759a66ce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "household_captures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("capture_text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "reviewed", sa.Boolean(), nullable=False, server_default="false",
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_household_captures_user_id_users", ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_household_captures_user_id", "household_captures", ["user_id"])
    op.create_index("ix_household_captures_kind", "household_captures", ["kind"])
    op.create_index("ix_household_captures_source", "household_captures", ["source"])
    op.create_index("ix_household_captures_reviewed", "household_captures", ["reviewed"])
    op.create_index("ix_household_captures_created_at", "household_captures", ["created_at"])

    op.create_table(
        "household_capture_source_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_ref", sa.String(length=200), nullable=False),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_household_capture_source_messages_user_id_users", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["household_captures.id"],
            name="fk_household_capture_source_messages_capture_id", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id", "message_ref",
            name="uq_household_capture_source_messages_user_ref",
        ),
    )
    op.create_index(
        "ix_household_capture_source_messages_user_id",
        "household_capture_source_messages", ["user_id"],
    )
    op.create_index(
        "ix_household_capture_source_messages_message_ref",
        "household_capture_source_messages", ["message_ref"],
    )
    op.create_index(
        "ix_household_capture_source_messages_capture_id",
        "household_capture_source_messages", ["capture_id"],
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_household_capture_source_messages_capture_id")
    op.execute("DROP INDEX IF EXISTS ix_household_capture_source_messages_message_ref")
    op.execute("DROP INDEX IF EXISTS ix_household_capture_source_messages_user_id")
    op.execute("DROP TABLE IF EXISTS household_capture_source_messages")

    op.execute("DROP INDEX IF EXISTS ix_household_captures_created_at")
    op.execute("DROP INDEX IF EXISTS ix_household_captures_reviewed")
    op.execute("DROP INDEX IF EXISTS ix_household_captures_source")
    op.execute("DROP INDEX IF EXISTS ix_household_captures_kind")
    op.execute("DROP INDEX IF EXISTS ix_household_captures_user_id")
    op.execute("DROP TABLE IF EXISTS household_captures")
