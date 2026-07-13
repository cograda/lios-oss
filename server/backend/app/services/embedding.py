"""Unified embedding pipeline — queue, worker, and search.

Replaces per-integration embedding logic with a single pipeline:
1. Integrations enqueue content via EmbeddingService.enqueue()
2. Background worker (every 5 min) processes the queue in batches
3. Unified 'embeddings' table enables cross-source semantic search

Model: BAAI/bge-small-en-v1.5 (384-dim) via fastembed.
"""

import hashlib
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, Index, or_
from sqlalchemy.orm import Mapped, Session, mapped_column

from coglib import Base

from app.auth.context import current_user_id_or_none

logger = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
VECTOR_DIM = 384

# Subprocess timeout for a single batch embed. A 100-item batch on CPU
# takes roughly 5–15s incl. ~2s fastembed init; 5 min is a generous ceiling.
EMBED_SUBPROCESS_TIMEOUT_SECONDS = 300

# Give up on a queue item after this many failed embed attempts.
MAX_EMBED_ATTEMPTS = 3

# Lazy-loaded model — only used by the live search() path. The batch worker
# now shells out to embed_subprocess.py so the main process doesn't retain
# the ONNX arenas between cycles.
_model = None


def get_model():
    """Get the shared fastembed model instance (lazy-loaded).

    Used by live search queries only. The batch worker uses
    _embed_via_subprocess() so model memory is reclaimed each cycle.
    """
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL_NAME)
    return _model


def _embed_via_subprocess(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts in a short-lived subprocess.

    The subprocess loads fastembed, embeds, writes vectors to stdout,
    and exits — the OS reclaims the ONNX/fastembed memory arenas
    rather than holding them in the long-running server process.
    """
    if not texts:
        return []

    payload = json.dumps(texts)
    proc = subprocess.run(
        [sys.executable, "-m", "app.services.embed_subprocess"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=EMBED_SUBPROCESS_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"embed_subprocess failed (rc={proc.returncode}): {proc.stderr[:500]}"
        )
    vectors = json.loads(proc.stdout)
    if len(vectors) != len(texts):
        raise RuntimeError(
            f"embed_subprocess returned {len(vectors)} vectors for {len(texts)} texts"
        )
    return vectors


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

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
    """Unified embedding vectors — all sources in one table."""

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
    embedding = mapped_column(Vector(VECTOR_DIM))
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_emb_source_source_id", "source", "source_id"),
        # HNSW so semantic search doesn't full-scan; build-once, no list
        # tuning needed (vs IVFFlat) as the table grows.
        Index(
            "ix_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class EmbeddingService:
    """Unified embedding pipeline: enqueue, process, search."""

    @staticmethod
    def enqueue(
        session: Session,
        source: str,
        source_id: str,
        content: str,
        metadata_json: str | None = None,
        user_id: int | None = None,
    ) -> bool:
        """Add content to the embedding queue.

        Deduplicates by content_hash — if the same content is already
        queued or embedded, skips it. If content changed (same source_id,
        different hash), queues for re-embedding.

        user_id=None means household-shared (visible to all users in
        search); per-user sources (email, whatsapp) must pass the owner.

        Returns True if enqueued, False if skipped (unchanged).
        """
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()

        # Check if we already have this exact content embedded
        existing = (
            session.query(Embedding)
            .filter_by(source=source, source_id=source_id, content_hash=content_hash)
            .first()
        )
        if existing:
            return False  # Already embedded with same content

        # Check if already queued with same content
        queued = (
            session.query(EmbeddingQueue)
            .filter_by(source=source, source_id=source_id, status="pending")
            .first()
        )
        if queued:
            if queued.content_hash == content_hash:
                return False  # Already queued with same content
            # Content changed — update the queue item
            queued.content = content
            queued.content_hash = content_hash
            queued.metadata_json = metadata_json
            queued.user_id = user_id
            queued.created_at = datetime.now(timezone.utc)
            session.flush()
            return True

        # Enqueue new item
        session.add(EmbeddingQueue(
            source=source,
            source_id=source_id,
            user_id=user_id,
            content=content,
            content_hash=content_hash,
            metadata_json=metadata_json,
            status="pending",
        ))
        session.flush()
        return True

    @staticmethod
    def enqueue_batch(
        session: Session,
        items: list[tuple[str, str, str, str | None]],
        user_id: int | None = None,
    ) -> int:
        """Enqueue multiple items efficiently.

        Each item is (source, source_id, content, metadata_json).
        user_id applies to every item in the batch (None = shared).
        Returns count of items actually enqueued (not skipped).
        """
        if not items:
            return 0

        # Precompute hashes
        prepared = []
        for source, source_id, content, metadata_json in items:
            content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
            prepared.append((source, source_id, content, content_hash, metadata_json))

        # Batch-check existing embeddings
        keys = [(s, sid) for s, sid, _, _, _ in prepared]
        existing_embeddings = {}
        for source, source_id in keys:
            row = (
                session.query(Embedding.content_hash)
                .filter_by(source=source, source_id=source_id)
                .first()
            )
            if row:
                existing_embeddings[(source, source_id)] = row.content_hash

        # Batch-check existing queue items
        existing_queue = {}
        for source, source_id in keys:
            row = (
                session.query(EmbeddingQueue.content_hash)
                .filter_by(source=source, source_id=source_id, status="pending")
                .first()
            )
            if row:
                existing_queue[(source, source_id)] = row.content_hash

        count = 0
        for source, source_id, content, content_hash, metadata_json in prepared:
            key = (source, source_id)

            # Skip if already embedded with same content
            if key in existing_embeddings and existing_embeddings[key] == content_hash:
                continue

            # Skip if already queued with same content
            if key in existing_queue and existing_queue[key] == content_hash:
                continue

            session.add(EmbeddingQueue(
                source=source,
                source_id=source_id,
                user_id=user_id,
                content=content,
                content_hash=content_hash,
                metadata_json=metadata_json,
                status="pending",
            ))
            count += 1

        if count:
            session.flush()

        return count

    @staticmethod
    def process_queue(session: Session, batch_size: int = 100) -> int:
        """Process pending items from the embedding queue.

        Fetches up to batch_size pending items, embeds them, and stores
        in the unified embeddings table. Removes old embeddings for items
        being re-embedded (content changed).

        Returns number of items processed.
        """
        # Reclaim orphans: process_queue is single-flight (scheduler runs it
        # with max_instances=1), so anything still 'processing' at entry was
        # abandoned by a killed run — put it back in the queue.
        orphaned = (
            session.query(EmbeddingQueue)
            .filter_by(status="processing")
            .update({"status": "pending"}, synchronize_session=False)
        )
        if orphaned:
            logger.warning(f"Reclaimed {orphaned} orphaned 'processing' queue items")
            session.commit()

        # Fetch pending items
        pending = (
            session.query(EmbeddingQueue)
            .filter_by(status="pending")
            .order_by(EmbeddingQueue.created_at)
            .limit(batch_size)
            .all()
        )

        if not pending:
            return 0

        # Mark as processing
        for item in pending:
            item.status = "processing"
        session.flush()

        t0 = time.time()
        done = EmbeddingService._embed_and_store(session, pending)
        elapsed = time.time() - t0
        logger.info(
            f"Embedded {done}/{len(pending)} items in {elapsed:.1f}s (subprocess)"
        )

        # Clean up old done/error items (keep last 24h for debugging)
        from sqlalchemy import and_
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        session.query(EmbeddingQueue).filter(
            and_(
                EmbeddingQueue.status.in_(["done", "error"]),
                EmbeddingQueue.processed_at < cutoff,
            )
        ).delete(synchronize_session=False)
        session.commit()

        return done

    @staticmethod
    def _embed_and_store(session: Session, items: list["EmbeddingQueue"]) -> int:
        """Embed a batch and store vectors; bisect on failure.

        Previously one bad item (or a transient subprocess failure) marked
        the whole batch 'error' with no retry. Now: a failed batch splits in
        half and each half retries, isolating the poison item. Attempts only
        increment at the single-item leaf — penalising the 99 innocent
        cohort members would error them out alongside the real culprit.

        Returns the number of items successfully embedded.
        """
        texts = [item.content for item in items]
        try:
            vectors = _embed_via_subprocess(texts)
        except Exception as e:
            if len(items) == 1:
                item = items[0]
                item.attempts = (item.attempts or 0) + 1
                item.error_message = str(e)[:500]
                if item.attempts >= MAX_EMBED_ATTEMPTS:
                    item.status = "error"
                    logger.error(
                        f"Embedding gave up on {item.source}:{item.source_id} "
                        f"after {item.attempts} attempts: {e}"
                    )
                else:
                    item.status = "pending"  # retried next cycle
                session.commit()
                return 0
            mid = len(items) // 2
            logger.warning(
                f"Embedding batch of {len(items)} failed ({type(e).__name__}) "
                f"— bisecting to isolate"
            )
            return (
                EmbeddingService._embed_and_store(session, items[:mid])
                + EmbeddingService._embed_and_store(session, items[mid:])
            )

        # Store embeddings and mark queue items done
        now = datetime.now(timezone.utc)
        for item, vec in zip(items, vectors):
            # Remove old embedding for this source+source_id (re-embedding case)
            session.query(Embedding).filter_by(
                source=item.source, source_id=item.source_id
            ).delete(synchronize_session=False)

            session.add(Embedding(
                source=item.source,
                source_id=item.source_id,
                user_id=item.user_id,
                chunk_index=0,
                chunk_text=item.content,
                embedding=vec,
                content_hash=item.content_hash,
                metadata_json=item.metadata_json,
                created_at=now,
            ))

            item.status = "done"
            item.processed_at = now

        session.commit()
        return len(items)

    @staticmethod
    def search(
        session: Session,
        query: str,
        sources: list[str] | None = None,
        limit: int = 10,
        source_filter: str | None = None,
    ) -> list[dict]:
        """Semantic search across the unified embeddings table.

        Args:
            query: Natural language search query.
            sources: List of sources to search (e.g. ["vault", "email"]).
                     None means all sources.
            limit: Max results.
            source_filter: Additional filter on source_id (e.g. folder path prefix).

        Results are user-scoped: rows owned by the bound user plus
        household-shared rows (user_id IS NULL). Unbound callers
        (background jobs) see shared rows only.

        Returns list of dicts with: source, source_id, score, preview, metadata.
        """
        model = get_model()
        q_vec = list(model.embed([query]))[0].tolist()

        q = session.query(
            Embedding.source,
            Embedding.source_id,
            Embedding.chunk_text,
            Embedding.metadata_json,
            Embedding.created_at,
            Embedding.embedding.cosine_distance(q_vec).label("distance"),
        )

        uid = current_user_id_or_none()
        if uid is not None:
            q = q.filter(or_(Embedding.user_id.is_(None), Embedding.user_id == uid))
        else:
            q = q.filter(Embedding.user_id.is_(None))

        if sources:
            q = q.filter(Embedding.source.in_(sources))

        if source_filter:
            q = q.filter(Embedding.source_id.ilike(f"{source_filter}%"))

        q = q.order_by("distance").limit(min(limit, 50))

        return [
            {
                "source": row.source,
                "source_id": row.source_id,
                "score": round(1 - row.distance, 4),
                "preview": row.chunk_text[:300] if row.chunk_text else "",
                "metadata": row.metadata_json,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in q.all()
        ]

    @staticmethod
    def delete_source(session: Session, source: str, source_id: str) -> int:
        """Remove embeddings for a source item (e.g. deleted vault file)."""
        count = (
            session.query(Embedding)
            .filter_by(source=source, source_id=source_id)
            .delete(synchronize_session=False)
        )
        # Also clean up any pending queue items
        session.query(EmbeddingQueue).filter_by(
            source=source, source_id=source_id, status="pending"
        ).delete(synchronize_session=False)
        session.flush()
        return count

    @staticmethod
    def stats(session: Session) -> dict:
        """Return embedding pipeline statistics."""
        total = session.query(func.count(Embedding.id)).scalar() or 0
        by_source = dict(
            session.query(Embedding.source, func.count(Embedding.id))
            .group_by(Embedding.source)
            .all()
        )
        queue_pending = (
            session.query(func.count(EmbeddingQueue.id))
            .filter_by(status="pending")
            .scalar() or 0
        )
        queue_error = (
            session.query(func.count(EmbeddingQueue.id))
            .filter_by(status="error")
            .scalar() or 0
        )
        latest = session.query(func.max(Embedding.created_at)).scalar()

        return {
            "model": MODEL_NAME,
            "dimensions": VECTOR_DIM,
            "total_embeddings": total,
            "by_source": by_source,
            "queue_pending": queue_pending,
            "queue_errors": queue_error,
            "last_embedded": latest.isoformat() if latest else None,
        }
