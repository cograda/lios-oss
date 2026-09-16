"""vault_read_grants — read-only cross-user vault access

Revision ID: 487b2290321a
Revises: 3f197d409143
Create Date: 2026-08-30

Adds the grant table only. Deliberately seeds **nothing**: a migration that
created a grant would hand one user another's vault as a side effect of
`alembic upgrade head`, which is not where an access decision belongs. Grants
are made explicitly with `python -m app.scripts.grant_vault_read`.
"""

from alembic import op
import sqlalchemy as sa


revision = "487b2290321a"
down_revision = "3f197d409143"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vault_read_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "grantee_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "grantee_user_id", "owner_user_id", "scope", name="uq_vault_read_grant",
        ),
    )
    op.create_index(
        "ix_vault_read_grants_grantee", "vault_read_grants", ["grantee_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_vault_read_grants_grantee", table_name="vault_read_grants")
    op.drop_table("vault_read_grants")
