"""sheet_exports — tracks Google Sheets comar created to mirror household
tables (e.g. the snag register) for members without MCP/vault access.

Household-shared (no user_id), like the tables it exports. owner_account_email
identifies whose OAuth token writes to the sheet.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sheet_exports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("spreadsheet_id", sa.String(length=100), nullable=False),
        sa.Column("spreadsheet_url", sa.Text(), nullable=False),
        sa.Column("owner_account_email", sa.String(length=200), nullable=False),
        sa.Column("shared_with", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_sheet_exports_key", "sheet_exports", ["key"], unique=True)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sheet_exports_key")
    op.execute("DROP TABLE IF EXISTS sheet_exports")
