"""doc_exports — tracks Google Docs comar created and owns.

Deliberately the same shape as `sheet_exports` (2026-07-20), because the
google_docs integration deliberately has the same contract as `sheets`:
create the file once per `key`, then overwrite its contents in place on
every later write so the URL and the shares survive.

Household-shared (no user_id), like `sheet_exports`. owner_account_email
identifies whose OAuth token writes to the document — and it is load-bearing
beyond credentials: the Drive `drive.file` scope is a *per-file* grant, so
only the account that created a document can replace its body. A rewrite
therefore has to use this column's account, not whoever happens to be
calling (see integrations/google_docs/tools.py::handle_write).

Revision ID: f7a2c4e9b1d3
Revises: d5e2b8f1a9c4
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7a2c4e9b1d3"
down_revision: Union[str, Sequence[str], None] = "d5e2b8f1a9c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "doc_exports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("document_id", sa.String(length=100), nullable=False),
        sa.Column("document_url", sa.Text(), nullable=False),
        sa.Column("owner_account_email", sa.String(length=200), nullable=False),
        sa.Column("shared_with", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_doc_exports_key", "doc_exports", ["key"], unique=True)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_doc_exports_key")
    op.execute("DROP TABLE IF EXISTS doc_exports")
