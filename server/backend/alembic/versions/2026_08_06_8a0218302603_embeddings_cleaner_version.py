"""Track which cleaner produced each chunk's text.

`model_name` on the vector tables records the embedder. Nothing recorded the
*text preparation*, so a corpus half-rewritten by a new cleaner looked exactly
like a clean one — which is how CLEANER_VERSION 1's misrouting of
`historical_corpus` stayed invisible.

Left NULL on backfill rather than defaulted to 1. Every existing row was
embedded *before* cleaning was wired into the enqueue chokepoint at all
(`app/integrations/embedding/cleaning.py` was imported by nothing until
`2ec1b5e`), so their text went through no cleaner. Stamping them "1" would
assert something false and make the Phase 4 backfill skip them; NULL says
"unknown, needs rewriting", which is both true and actionable.

Revision ID: 8a0218302603
Revises: 715ec8c41670
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa

revision = "8a0218302603"
down_revision = "715ec8c41670"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "embeddings", sa.Column("cleaner_version", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_embeddings_cleaner_version", "embeddings", ["cleaner_version"]
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_embeddings_cleaner_version")
    op.drop_column("embeddings", "cleaner_version")
