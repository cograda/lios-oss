"""embeddings_hnsw_index: vector index for semantic search

The embeddings table had no vector index, so every semantic search was a
sequential scan with the cosine operator. HNSW (default m=16,
ef_construction=64) is build-once / insert-forever — no IVFFlat list
re-tuning as the table grows past the current ~4K rows.

Plain CREATE INDEX (not CONCURRENTLY — alembic runs in a transaction);
the brief lock is a non-issue at this table size.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw "
        "ON embeddings USING hnsw (embedding vector_cosine_ops)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_embeddings_hnsw"))
