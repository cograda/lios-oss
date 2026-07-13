"""Embedding pipeline suite (db tier) — enqueue → process → search.

Mocks exactly one seam: `_embed_via_subprocess` (the fastembed subprocess).
Everything else — queue state machine, bisection, pgvector storage and
cosine search, user scoping — runs against real Postgres.
"""

import pytest

from app.auth.context import _current_user_id, use_user
from app.services import embedding as emb
from app.services.embedding import Embedding, EmbeddingQueue, EmbeddingService

pytestmark = pytest.mark.db


def _vec(x: float = 1.0) -> list[float]:
    """A 384-dim unit-ish vector with all weight on the first component."""
    v = [0.0] * emb.VECTOR_DIM
    v[0] = x
    v[1] = 1.0 - abs(x)
    return v


@pytest.fixture
def fake_embedder(monkeypatch):
    """Deterministic stand-in for the fastembed subprocess.

    Raises for any text containing 'POISON' — the hook for bisection tests.
    """
    calls = []

    def embed(texts):
        calls.append(list(texts))
        for t in texts:
            if "POISON" in t:
                raise RuntimeError(f"poison text: {t[:30]}")
        return [_vec(1.0) for _ in texts]

    monkeypatch.setattr(emb, "_embed_via_subprocess", embed)
    return calls


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def test_enqueue_dedups_unchanged_content(db_session):
    assert EmbeddingService.enqueue(db_session, "vault", "a.md", "hello") is True
    assert EmbeddingService.enqueue(db_session, "vault", "a.md", "hello") is False
    assert db_session.query(EmbeddingQueue).count() == 1


def test_enqueue_updates_pending_item_on_content_change(db_session):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "v1", user_id=None)
    assert EmbeddingService.enqueue(db_session, "vault", "a.md", "v2") is True

    rows = db_session.query(EmbeddingQueue).all()
    assert len(rows) == 1
    assert rows[0].content == "v2"


def test_enqueue_skips_content_already_embedded(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)
    # Same content again — already in embeddings, not re-queued.
    assert EmbeddingService.enqueue(db_session, "vault", "a.md", "hello") is False
    assert (
        db_session.query(EmbeddingQueue).filter_by(status="pending").count() == 0
    )


def test_enqueue_batch_counts_and_threads_user(db_session):
    items = [
        ("email", "m1", "first message", None),
        ("email", "m2", "second message", None),
    ]
    assert EmbeddingService.enqueue_batch(db_session, items, user_id=2) == 2
    assert EmbeddingService.enqueue_batch(db_session, items, user_id=2) == 0

    for row in db_session.query(EmbeddingQueue).all():
        assert row.user_id == 2


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------

def test_process_queue_embeds_and_threads_user_id(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "email", "m1", "user one mail", user_id=1)
    EmbeddingService.enqueue(db_session, "vault", "a.md", "shared note", user_id=None)

    assert EmbeddingService.process_queue(db_session) == 2

    by_source = {e.source: e for e in db_session.query(Embedding).all()}
    assert by_source["email"].user_id == 1
    assert by_source["vault"].user_id is None
    statuses = {q.status for q in db_session.query(EmbeddingQueue).all()}
    assert statuses == {"done"}


def test_reembed_replaces_old_vector(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "v1")
    EmbeddingService.process_queue(db_session)
    EmbeddingService.enqueue(db_session, "vault", "a.md", "v2")
    EmbeddingService.process_queue(db_session)

    rows = db_session.query(Embedding).filter_by(source="vault", source_id="a.md").all()
    assert len(rows) == 1
    assert rows[0].chunk_text == "v2"


def test_orphaned_processing_items_are_reclaimed(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "abandoned")
    db_session.query(EmbeddingQueue).update({"status": "processing"})
    db_session.commit()

    assert EmbeddingService.process_queue(db_session) == 1
    assert db_session.query(EmbeddingQueue).filter_by(status="done").count() == 1


# ---------------------------------------------------------------------------
# Poison isolation
# ---------------------------------------------------------------------------

def test_poison_item_is_bisected_out(db_session, fake_embedder):
    for i in range(4):
        EmbeddingService.enqueue(db_session, "vault", f"ok{i}.md", f"fine {i}")
    EmbeddingService.enqueue(db_session, "vault", "bad.md", "POISON pill")

    done = EmbeddingService.process_queue(db_session)

    assert done == 4  # the four innocents embedded despite the poison
    bad = db_session.query(EmbeddingQueue).filter_by(source_id="bad.md").one()
    assert bad.status == "pending"  # retried next cycle
    assert bad.attempts == 1
    assert "poison" in (bad.error_message or "")


def test_poison_item_errors_out_after_max_attempts(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "vault", "bad.md", "POISON pill")

    for _ in range(emb.MAX_EMBED_ATTEMPTS):
        EmbeddingService.process_queue(db_session)

    bad = db_session.query(EmbeddingQueue).filter_by(source_id="bad.md").one()
    assert bad.status == "error"
    assert bad.attempts == emb.MAX_EMBED_ATTEMPTS

    # An errored item is no longer picked up.
    assert EmbeddingService.process_queue(db_session) == 0


# ---------------------------------------------------------------------------
# Search — pgvector + user scoping
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_search(db_session, monkeypatch):
    """Three embeddings: user 1's, user 2's, household-shared (NULL).

    The query embedder is stubbed to a fixed vector; relevance ordering is
    controlled by how close each row's vector is to it.
    """
    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.array(_vec(1.0), dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())

    db_session.add_all([
        Embedding(source="email", source_id="u1-mail", user_id=1,
                  chunk_text="user one secret", embedding=_vec(1.0),
                  content_hash="h1"),
        Embedding(source="email", source_id="u2-mail", user_id=2,
                  chunk_text="user two secret", embedding=_vec(0.95),
                  content_hash="h2"),
        Embedding(source="vault", source_id="shared.md", user_id=None,
                  chunk_text="household note", embedding=_vec(0.9),
                  content_hash="h3"),
    ])
    db_session.commit()
    return db_session


def test_search_returns_own_and_shared_only(seeded_search):
    with use_user(1):
        results = EmbeddingService.search(seeded_search, "anything")

    ids = [r["source_id"] for r in results]
    assert "u1-mail" in ids
    assert "shared.md" in ids
    assert "u2-mail" not in ids


def test_search_other_user_sees_their_own(seeded_search):
    with use_user(2):
        ids = [r["source_id"] for r in EmbeddingService.search(seeded_search, "x")]
    assert "u2-mail" in ids and "u1-mail" not in ids and "shared.md" in ids


def test_search_unbound_sees_shared_only(seeded_search):
    token = _current_user_id.set(0)  # background-job context: no user bound
    try:
        ids = [r["source_id"] for r in EmbeddingService.search(seeded_search, "x")]
    finally:
        _current_user_id.reset(token)
    assert ids == ["shared.md"]


def test_search_orders_by_cosine_distance(seeded_search):
    with use_user(1):
        results = EmbeddingService.search(seeded_search, "anything")
    # u1-mail's vector equals the query vector → must rank first.
    assert results[0]["source_id"] == "u1-mail"
    assert results[0]["score"] >= results[-1]["score"]


def test_search_filters_by_source(seeded_search):
    with use_user(1):
        results = EmbeddingService.search(seeded_search, "x", sources=["vault"])
    assert {r["source_id"] for r in results} == {"shared.md"}


def test_delete_source_removes_vector_and_pending(db_session, fake_embedder):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "v1")
    EmbeddingService.process_queue(db_session)
    EmbeddingService.enqueue(db_session, "vault", "a.md", "v2")  # pending again

    EmbeddingService.delete_source(db_session, "vault", "a.md")
    db_session.commit()

    assert db_session.query(Embedding).filter_by(source_id="a.md").count() == 0
    assert (
        db_session.query(EmbeddingQueue)
        .filter_by(source_id="a.md", status="pending").count() == 0
    )
