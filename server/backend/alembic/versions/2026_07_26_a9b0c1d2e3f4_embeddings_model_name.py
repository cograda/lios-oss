"""embeddings.model_name — per-row provider/model identifier (V4 chunk 3.4).

The embeddings table has always been produced by exactly one model
(fastembed BAAI/bge-small-en-v1.5), so there was never a reason to record
which one made a given row. Chunk 3.4 introduces a swappable
`EmbeddingProvider` interface (`app/plugin/embedding_provider.py`) — a future
provider swap needs some way to tell "already embedded with the new model"
apart from "still on the old one" without re-embedding blind. This migration
just makes that detectable: adds the column and backfills every existing row
with the model that actually produced it. No re-embed pipeline yet — that's
explicitly out of scope for this chunk.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every row in `embeddings` prior to this migration was produced by this
# model (see app/plugin/embedding_provider.py::FastEmbedProvider.model_name).
_LEGACY_MODEL_NAME = "BAAI/bge-small-en-v1.5"


def upgrade() -> None:
    op.add_column("embeddings", sa.Column("model_name", sa.String(length=200), nullable=True))
    op.execute(f"UPDATE embeddings SET model_name = '{_LEGACY_MODEL_NAME}' WHERE model_name IS NULL")
    op.create_index("ix_embeddings_model_name", "embeddings", ["model_name"])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_embeddings_model_name")
    op.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS model_name")
