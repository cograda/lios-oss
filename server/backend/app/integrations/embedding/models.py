"""Embedding models — unified queue + one vector table per embedding space.

Moved out of `app/services/embedding.py` (V4 chunk 3.4) so the `embedding`
capability package owns them like every other integration owns its models
(declared via `manifest.py::MANIFEST.models`, picked up by
`app.plugin.discovery.discover_integration_models()`). `services/embedding.py`
re-exports the classes for every existing importer — see that module's
top-of-file comment.

## Why the vectors don't live on `embeddings` any more (Phase 2)

pgvector fixes the dimension **in the column type**, so a second embedding
model cannot be a second row — it has to be a second column or a second table.
Comar now runs two spaces (gemini-embedding-2 at 1536d as the search default,
bge-small at 384d as the offline/rate-limited fallback), so `embeddings` keeps
only chunk identity and text, and each space gets its own table:

    embeddings                      identity + chunk_text, no vector
    embedding_vec_gemini_1536       embedding_id -> Vector(1536)
    embedding_vec_bge_small_384     embedding_id -> Vector(384)

One table per space rather than one column per space because:

- **Coverage is a COUNT and a gap is an anti-join.** "Which rows still need
  Gemini" is `LEFT JOIN … WHERE vec.embedding_id IS NULL`, not a scan for NULLs
  in a wide row. Phase 4's re-enqueue pass is driven by exactly that query.
- **Each index is sized to its own space.** HNSW on 1536d costs roughly 4x the
  memory per row of 384d; a shared table would make both indexes span every row
  whether or not that space had a vector for it. This box has run out of disk
  once already.
- **Adding or swapping a space stays a migration, not a redesign** — and
  swapping the local model changes its dimension, so this will happen again.

**The invariant that makes two spaces sane: one space per query, end to end.**
A fallback embeds the query into a space and answers entirely within it. Cosine
distance between vectors from different models is meaningless — never join
across these tables, never mix their scores.
"""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, Index
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from coglib import Base


class EmbeddingQueue(Base):
    """Items waiting to be embedded."""

    __tablename__ = "embedding_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), index=True)  # vault, email, whatsapp, ...
    source_id: Mapped[str] = mapped_column(String(500), index=True)  # unique key within source
    # NULL = household-shared (vault, corpus, coffee). Set for per-user
    # sources (email, whatsapp) so search can scope results.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    content: Mapped[str] = mapped_column(Text)  # text to embed
    content_hash: Mapped[str] = mapped_column(String(64))  # MD5 for dedup
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # optional JSON metadata
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending/processing/done/error
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # failed embed attempts
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_eq_source_source_id", "source", "source_id"),
        Index("ix_eq_status_created", "status", "created_at"),
    )


class Embedding(Base):
    """A chunk of embeddable content — identity and text, no vector.

    The vectors live in the per-space tables below, one row each, keyed on
    `id`. A row here with no matching vector row anywhere is content that has
    been queued and stored but not yet embedded into any space; a row with one
    of two is the normal partial state a backfill closes.
    """

    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    source_id: Mapped[str] = mapped_column(String(500), index=True)
    # NULL = household-shared; see EmbeddingQueue.user_id.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)  # for multi-chunk items
    chunk_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which cleaner produced `chunk_text`. `model_name` on the vector tables
    # tracks the embedder; nothing tracked the *text preparation*, so a corpus
    # half-rewritten by a new cleaner was indistinguishable from a clean one.
    # Nullable: rows predating the column are exactly the ones whose version is
    # genuinely unknown, and NULL says that honestly where a default would lie.
    cleaner_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_emb_source_source_id", "source", "source_id"),
    )


class _VectorSpaceMixin:
    """Shared shape for every per-space vector table.

    `embedding_id` is both primary key and FK — one vector per chunk per space,
    enforced by the schema rather than by convention. `ON DELETE CASCADE` means
    `EmbeddingService.delete_source()` keeps working unchanged: deleting the
    `embeddings` row drops its vectors in every space, with no per-space
    cleanup to forget when a third space is added.

    `model_name` is kept per row even though the table already implies the
    model, because a space can have its model upgraded in place at the same
    width (gemini-embedding-2 -> a hypothetical 2.1). Without it, "which rows
    in this space are stale" would be unanswerable without re-embedding to find
    out.
    """

    @declared_attr
    def embedding_id(cls) -> Mapped[int]:
        return mapped_column(
            ForeignKey("embeddings.id", ondelete="CASCADE"), primary_key=True
        )

    @declared_attr
    def model_name(cls) -> Mapped[str]:
        return mapped_column(String(200), nullable=False, index=True)

    @declared_attr
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(DateTime(timezone=True), server_default=func.now())


class EmbeddingVecGemini1536(_VectorSpaceMixin, Base):
    """gemini-embedding-2, 1536d (Matryoshka-truncated from 3072)."""

    __tablename__ = "embedding_vec_gemini_1536"

    embedding = mapped_column(Vector(1536))

    __table_args__ = (
        # HNSW so semantic search doesn't full-scan; build-once, no list
        # tuning needed (vs IVFFlat) as the table grows.
        Index(
            "ix_emb_vec_gemini_1536_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class EmbeddingVecBgeSmall384(_VectorSpaceMixin, Base):
    """BAAI/bge-small-en-v1.5, 384d — the local, offline-capable fallback."""

    __tablename__ = "embedding_vec_bge_small_384"

    embedding = mapped_column(Vector(384))

    __table_args__ = (
        Index(
            "ix_emb_vec_bge_small_384_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# provider id (the string in HOME_EMBEDDING_PROVIDER) -> its vector table.
#
# Keyed on provider id rather than model name because the provider id is what
# operators actually set, and `app.plugin.embedding_provider` deliberately
# keeps ids stable across model upgrades. `app/services/embedding.py` asserts
# at import time that this mapping and `_PROVIDERS` cover the same ids — a
# provider with nowhere to write is a boot-time error, not a runtime surprise.
VECTOR_MODELS: dict[str, type] = {
    "gemini-embedding-2": EmbeddingVecGemini1536,
    "fastembed-bge-small": EmbeddingVecBgeSmall384,
}
