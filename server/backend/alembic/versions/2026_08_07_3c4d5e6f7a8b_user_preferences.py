"""user_preferences — per-user display/shape preferences

Backs `app.services.preferences`. Distinct from `integration_config`, which is
per-deployment: two people on one server share a commute route but not a
daily-note layout.

Revision ID: 3c4d5e6f7a8b
Revises: 0f1e2d3c4b5a
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa

revision = "3c4d5e6f7a8b"
down_revision = "0f1e2d3c4b5a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("user_id", "key", name="uq_user_preferences_user_key"),
    )
    op.create_index(
        "ix_user_preferences_user_id", "user_preferences", ["user_id"],
    )
    op.create_index(
        "ix_user_preferences_key", "user_preferences", ["key"],
    )


def downgrade() -> None:
    # IF EXISTS throughout — see user-memory `feedback_alembic_migration_safety`:
    # a partially-applied upgrade must still be reversible.
    op.drop_index("ix_user_preferences_key", table_name="user_preferences", if_exists=True)
    op.drop_index("ix_user_preferences_user_id", table_name="user_preferences", if_exists=True)
    op.drop_table("user_preferences", if_exists=True)
