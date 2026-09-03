"""Split embedding vectors into one table per embedding space.

`embeddings` keeps chunk identity and text; the vector moves to a per-space
table keyed on `embeddings.id`. pgvector fixes the dimension in the column
type, so a second model (gemini-embedding-2 at 1536d alongside bge-small at
384d) cannot share a column — see app/integrations/embedding/models.py for the
full rationale.

Ordering here is deliberate:

1. Create the vector tables with NO index. Bulk-inserting into an existing HNSW
   index is dramatically slower than building the index once at the end.
2. Copy the existing 384d vectors across.
3. Build the HNSW indexes.
4. Only then drop the old column, so a failure at any earlier step rolls back
   with the original data still in place (Postgres DDL is transactional).

`model_name` is NOT NULL on the vector tables, so the copy backfills any legacy
NULLs with the model those rows were actually built by — every vector in this
database predates the second provider, so bge-small is correct by construction
rather than by assumption.

Revision ID: 715ec8c41670
Revises: a7b8c9d0e1f2
Create Date: 2026-08-06

NB the random revision id, breaking this repo's house style of rotating hex
patterns (a3b4c5d6e7f8 -> b4c5d6e7f8a9 -> ...). That sequence wraps: continuing
it by hand picked `b8c9d0e1f2a3`, which was already used on 2026-07-02 by
`home_assistant_state`. Alembic reports a duplicate id as a **cycle**, naming
every revision in the graph and pointing nowhere near the actual collision.
Random ids can't collide by pattern exhaustion; prefer them here.
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "715ec8c41670"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None

LEGACY_MODEL = "BAAI/bge-small-en-v1.5"


def _create_space_table(name: str, dim: int) -> None:
    op.create_table(
        name,
        sa.Column(
            "embedding_id",
            sa.Integer(),
            sa.ForeignKey("embeddings.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("embedding", Vector(dim)),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(f"ix_{name}_model_name", name, ["model_name"])


def upgrade() -> None:
    _create_space_table("embedding_vec_bge_small_384", 384)
    _create_space_table("embedding_vec_gemini_1536", 1536)

    # Carry every existing vector into the local space. COALESCE because
    # model_name was nullable and pre-3.4 rows may never have been backfilled;
    # they are bge-small regardless, as no other provider has ever written here.
    op.execute(
        f"""
        INSERT INTO embedding_vec_bge_small_384
            (embedding_id, embedding, model_name, created_at)
        SELECT id, embedding, COALESCE(model_name, '{LEGACY_MODEL}'), created_at
        FROM embeddings
        WHERE embedding IS NOT NULL
        """
    )

    # Built after the copy, not before — see module docstring.
    op.execute(
        "CREATE INDEX ix_emb_vec_bge_small_384_hnsw "
        "ON embedding_vec_bge_small_384 "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_emb_vec_gemini_1536_hnsw "
        "ON embedding_vec_gemini_1536 "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute("DROP INDEX IF EXISTS ix_embeddings_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_embeddings_model_name")
    op.drop_column("embeddings", "embedding")
    op.drop_column("embeddings", "model_name")


def downgrade() -> None:
    op.add_column("embeddings", sa.Column("embedding", Vector(384)))
    op.add_column("embeddings", sa.Column("model_name", sa.String(200), nullable=True))

    # Only the local space can come back — a 1536d vector does not fit a 384d
    # column, so downgrading discards the gemini space rather than corrupting
    # it. That is the honest behaviour: the data is re-derivable by re-embedding.
    op.execute(
        """
        UPDATE embeddings e
        SET embedding = v.embedding,
            model_name = v.model_name
        FROM embedding_vec_bge_small_384 v
        WHERE v.embedding_id = e.id
        """
    )

    op.execute(
        "CREATE INDEX ix_embeddings_hnsw ON embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.create_index("ix_embeddings_model_name", "embeddings", ["model_name"])

    op.drop_table("embedding_vec_gemini_1536")
    op.drop_table("embedding_vec_bge_small_384")
