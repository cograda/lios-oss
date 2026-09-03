"""integration_config — per-integration config/secret store (V4 chunk 3.3).

New table only; nothing existing migrates onto it here. The one-time env->DB
copy is a manual command (`python -m app.plugin.import_config`), not part of
this migration, per the chunk 3.3 spec ("management command", operator-run).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "integration_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("integration", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_by", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.create_index("ix_integration_config_integration", "integration_config", ["integration"])
    op.create_unique_constraint(
        "uq_integration_config_integration_key",
        "integration_config", ["integration", "key"],
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE integration_config DROP CONSTRAINT IF EXISTS "
        "uq_integration_config_integration_key"
    )
    op.execute("DROP INDEX IF EXISTS ix_integration_config_integration")
    op.drop_table("integration_config")
