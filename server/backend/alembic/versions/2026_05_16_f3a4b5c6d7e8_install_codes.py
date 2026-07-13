"""install_codes: single-use, time-limited install bootstraps for new machines

A new (non-developer) machine has no comar credentials and shouldn't need to
paste a bearer token. The admin mints a `client_tokens` row plus a row here
that references it; the new machine then hits `GET /api/install/<code>` and
gets a personalised install script with the token baked in. Single-use
(redeemed_at set on first fetch) and 24h-TTL (expires_at).

The route trusts the code itself — no other auth — so the schema is the
boundary: unique index on `code`, RESTRICT FKs to users/client_tokens so a
token can't be deleted out from under an unredeemed code.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "install_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_from_ip", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["token_id"], ["client_tokens.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_install_codes_code"),
    )
    op.create_index("ix_install_codes_code", "install_codes", ["code"], unique=False)
    op.create_index("ix_install_codes_user_id", "install_codes", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_install_codes_user_id", table_name="install_codes")
    op.drop_index("ix_install_codes_code", table_name="install_codes")
    op.drop_table("install_codes")
