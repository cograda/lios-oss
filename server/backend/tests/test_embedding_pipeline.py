"""Embedding pipeline suite (db tier) — enqueue → process → search.

Mocks exactly one seam: `_embed_via_subprocess` (the fastembed subprocess).
Everything else — queue state machine, bisection, pgvector storage and
cosine search, user scoping — runs against real Postgres.
"""

import pytest

from app.auth.context import _current_user_id, use_user
from app.services import embedding as emb
from app.services.embedding import (
    Embedding,
    EmbeddingQueue,
    EmbeddingService,
    EmbeddingVecBgeSmall384,
    EmbeddingVecGemini1536,
)

pytestmark = pytest.mark.db


def _vec(x: float = 1.0) -> list[float]:
    """A 384-dim unit-ish vector with all weight on the first component."""
    v = [0.0] * emb.VECTOR_DIM
    v[0] = x
    v[1] = 1.0 - abs(x)
    return v


def _seed(session, source, source_id, user_id, text, vec, content_hash):
    """Insert a chunk plus its vector in the local space.

    Since Phase 2 the vector lives in a per-space table rather than on
    `embeddings`, so seeding takes two rows and a flush to get the id.
    """
    row = Embedding(
        source=source, source_id=source_id, user_id=user_id,
        chunk_text=text, content_hash=content_hash,
    )
    session.add(row)
    session.flush()
    session.add(EmbeddingVecBgeSmall384(
        embedding_id=row.id, embedding=vec, model_name=emb.MODEL_NAME,
    ))
    return row


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


def test_reembed_batch_only_replaces_matching_rows(db_session, fake_embedder):
    """P5 (hardening-2026-08.md): the supersede-delete pass batches every
    item in a `_embed_and_store` call into one statement (a composite
    `tuple_(source, source_id).in_(...)` DELETE) instead of one DELETE per
    item. Prove the batching didn't turn into "delete everything in the
    batch's tables" — each of three sources gets re-embedded in the same
    `process_queue` call, and only the row matching its own (source,
    source_id) should be replaced; the other two must survive untouched.
    """
    for sid, text in [("a.md", "v1"), ("b.md", "v1"), ("c.md", "v1")]:
        EmbeddingService.enqueue(db_session, "vault", sid, text)
    assert EmbeddingService.process_queue(db_session) == 3

    # Re-embed all three as a single batch (one process_queue call = one
    # _embed_and_store call over all pending rows).
    for sid, text in [("a.md", "v2"), ("b.md", "v2"), ("c.md", "v2")]:
        EmbeddingService.enqueue(db_session, "vault", sid, text)
    assert EmbeddingService.process_queue(db_session) == 3

    rows = db_session.query(Embedding).filter_by(source="vault").all()
    by_id = {r.source_id: r.chunk_text for r in rows}
    assert by_id == {"a.md": "v2", "b.md": "v2", "c.md": "v2"}
    assert len(rows) == 3  # no duplicates, no cross-item deletion


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

    _seed(db_session, "email", "u1-mail", 1, "user one secret", _vec(1.0), "h1")
    _seed(db_session, "email", "u2-mail", 2, "user two secret", _vec(0.95), "h2")
    _seed(db_session, "vault", "shared.md", None, "household note", _vec(0.9), "h3")
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


def test_stats_reports_per_space_coverage(db_session, fake_embedder):
    """Phase 2: coverage is per space, and `missing` is the backfill's worklist."""
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)

    stats = EmbeddingService.stats(db_session)
    spaces = {s["provider"]: s for s in stats["spaces"]}

    # The configured local space has the row...
    local = spaces["fastembed-bge-small"]
    assert local["vectors"] == 1
    assert local["missing"] == 0
    assert local["by_model"] == {emb.MODEL_NAME: 1}
    assert local["active"] is True

    # ...and the unconfigured one is reported as an empty, inactive gap rather
    # than omitted — a space you can't see is a backfill you never run.
    remote = spaces["gemini-embedding-2"]
    assert remote["vectors"] == 0
    assert remote["missing"] == 1
    assert remote["active"] is False


def test_same_source_id_for_two_users_stays_separate(db_session, fake_embedder):
    """Two vaults both holding `Inbox/note.md` must not collide.

    Identity is (source, source_id, user_id): the second enqueue is a
    different item, not a duplicate of the first, and deleting one owner's
    copy must leave the other's intact.
    """
    assert EmbeddingService.enqueue(
        db_session, "vault", "Inbox/note.md", "alex's note", user_id=1,
    )
    assert EmbeddingService.enqueue(
        db_session, "vault", "Inbox/note.md", "sam's note", user_id=2,
    )
    EmbeddingService.process_queue(db_session)
    db_session.commit()

    owners = {
        e.user_id
        for e in db_session.query(Embedding).filter_by(source_id="Inbox/note.md").all()
    }
    assert owners == {1, 2}

    # Scoped delete removes only the named owner's copy.
    EmbeddingService.delete_source(
        db_session, "vault", "Inbox/note.md", 1, scope_user=True,
    )
    db_session.commit()

    survivors = db_session.query(Embedding).filter_by(source_id="Inbox/note.md").all()
    assert [e.user_id for e in survivors] == [2]


def test_vault_search_does_not_cross_vaults(db_session, fake_embedder, monkeypatch):
    """Regression: vault chunks were enqueued with no owner (NULL = shared),
    so a second user's vault_search returned the first user's whole vault."""
    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.array(_vec(1.0), dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())

    EmbeddingService.enqueue(
        db_session, "vault", "Health/MRI.md", "private knee report", user_id=1,
    )
    EmbeddingService.enqueue(
        db_session, "vault", "Notes/greenhouse.md", "greenhouse plan", user_id=2,
    )
    EmbeddingService.process_queue(db_session)
    db_session.commit()

    with use_user(2):
        ids = {
            r["source_id"]
            for r in EmbeddingService.search(db_session, "anything", sources=["vault"])
        }
    assert ids == {"Notes/greenhouse.md"}


# ---------------------------------------------------------------------------
# Phase 2 — multiple vector spaces
# ---------------------------------------------------------------------------

class _FakeRemoteProvider:
    """Stands in for GeminiEmbeddingProvider: a 1536-dim remote space.

    Deliberately NOT a FastEmbedProvider subclass, so it exercises the other
    branch of `_embed_with`/`_embed_query_with` — the one that calls the
    provider directly instead of shelling out to the subprocess.
    """

    provider_id = "gemini-embedding-2"
    model_name = "gemini-embedding-2"
    dim = 1536

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.queries: list[str] = []

    def available(self) -> bool:
        return True

    def _v(self, x: float = 1.0) -> list[float]:
        v = [0.0] * self.dim
        v[0] = x
        return v

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("remote space is down")
        return [self._v() for _ in texts]

    def embed_query(self, text):
        self.queries.append(text)
        if self.fail:
            raise RuntimeError("remote space is down")
        return self._v()


@pytest.fixture
def two_spaces(monkeypatch, fake_embedder):
    """Configure both spaces, remote first (the production ordering)."""
    from app.plugin.embedding_provider import FastEmbedProvider

    remote = _FakeRemoteProvider()
    monkeypatch.setattr(emb, "_active_spaces", lambda: [
        (remote, EmbeddingVecGemini1536),
        (FastEmbedProvider(), EmbeddingVecBgeSmall384),
    ])
    return remote


def test_both_spaces_are_written(db_session, two_spaces):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    assert EmbeddingService.process_queue(db_session) == 1

    row = db_session.query(Embedding).filter_by(source_id="a.md").one()
    assert db_session.query(EmbeddingVecGemini1536).filter_by(
        embedding_id=row.id).count() == 1
    assert db_session.query(EmbeddingVecBgeSmall384).filter_by(
        embedding_id=row.id).count() == 1


def test_one_space_failing_does_not_block_the_other(db_session, monkeypatch, fake_embedder):
    """A remote outage must not stop the local space embedding.

    This is the whole point of keeping a local fallback — requiring every
    space would make the remote provider a single point of failure for the
    thing that exists to survive it.
    """
    from app.plugin.embedding_provider import FastEmbedProvider

    monkeypatch.setattr(emb, "_active_spaces", lambda: [
        (_FakeRemoteProvider(fail=True), EmbeddingVecGemini1536),
        (FastEmbedProvider(), EmbeddingVecBgeSmall384),
    ])

    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    assert EmbeddingService.process_queue(db_session) == 1

    row = db_session.query(Embedding).filter_by(source_id="a.md").one()
    assert db_session.query(EmbeddingVecBgeSmall384).filter_by(
        embedding_id=row.id).count() == 1
    assert db_session.query(EmbeddingVecGemini1536).count() == 0

    # The gap is visible and sized, not silent.
    spaces = {s["provider"]: s for s in EmbeddingService.stats(db_session)["spaces"]}
    assert spaces["gemini-embedding-2"]["missing"] == 1


def test_all_spaces_failing_still_errors_the_item(db_session, monkeypatch):
    """With no space succeeding, the old bisect/retry semantics must hold."""
    monkeypatch.setattr(emb, "_active_spaces", lambda: [
        (_FakeRemoteProvider(fail=True), EmbeddingVecGemini1536),
    ])

    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    for _ in range(emb.MAX_EMBED_ATTEMPTS):
        EmbeddingService.process_queue(db_session)

    assert db_session.query(EmbeddingQueue).filter_by(status="error").count() == 1
    assert db_session.query(Embedding).count() == 0


def test_search_answers_from_the_first_space_and_names_it(db_session, two_spaces):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)

    results = EmbeddingService.search(db_session, "anything", sources=["vault"])

    assert [r["source_id"] for r in results] == ["a.md"]
    # Answered by the remote space, and says so — a fallback that doesn't
    # announce itself reads as the primary model quietly degrading.
    assert results[0]["space"] == "gemini-embedding-2"
    assert two_spaces.queries == ["anything"]


def test_search_falls_through_to_the_local_space(db_session, monkeypatch, fake_embedder):
    """Primary configured but broken → answer from the fallback, and say so."""
    from app.plugin.embedding_provider import FastEmbedProvider

    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.array(_vec(1.0), dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())

    working = _FakeRemoteProvider()
    monkeypatch.setattr(emb, "_active_spaces", lambda: [
        (working, EmbeddingVecGemini1536),
        (FastEmbedProvider(), EmbeddingVecBgeSmall384),
    ])
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)

    # Now the remote provider goes down for querying only.
    working.fail = True
    results = EmbeddingService.search(db_session, "anything", sources=["vault"])

    assert [r["source_id"] for r in results] == ["a.md"]
    assert results[0]["space"] == emb.MODEL_NAME


def test_search_skips_a_configured_but_unbackfilled_space(db_session, monkeypatch, fake_embedder):
    """A space with zero vectors must not answer every query with nothing.

    This is the state right after adding a provider and before the backfill
    runs — the most likely moment for search to silently go blank.
    """
    from app.plugin.embedding_provider import FastEmbedProvider

    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.array(_vec(1.0), dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())

    # Write to the local space only...
    monkeypatch.setattr(emb, "_active_spaces", lambda: [
        (FastEmbedProvider(), EmbeddingVecBgeSmall384),
    ])
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)

    # ...then bring an empty remote space online ahead of it.
    remote = _FakeRemoteProvider()
    monkeypatch.setattr(emb, "_active_spaces", lambda: [
        (remote, EmbeddingVecGemini1536),
        (FastEmbedProvider(), EmbeddingVecBgeSmall384),
    ])

    results = EmbeddingService.search(db_session, "anything", sources=["vault"])
    assert [r["source_id"] for r in results] == ["a.md"]
    assert results[0]["space"] == emb.MODEL_NAME
    assert remote.queries == []  # never even embedded the query


def test_deleting_a_chunk_cascades_to_every_space(db_session, two_spaces):
    """Vector rows go with their chunk via ON DELETE CASCADE.

    Structural on purpose: a third space must not require remembering to add
    another cleanup call to delete_source().
    """
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)

    EmbeddingService.delete_source(db_session, "vault", "a.md")
    db_session.commit()

    assert db_session.query(EmbeddingVecGemini1536).count() == 0
    assert db_session.query(EmbeddingVecBgeSmall384).count() == 0


def test_reembed_leaves_exactly_one_vector_per_space(db_session, two_spaces):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "v1")
    EmbeddingService.process_queue(db_session)
    EmbeddingService.enqueue(db_session, "vault", "a.md", "v2")
    EmbeddingService.process_queue(db_session)

    assert db_session.query(Embedding).filter_by(source_id="a.md").count() == 1
    assert db_session.query(EmbeddingVecGemini1536).count() == 1
    assert db_session.query(EmbeddingVecBgeSmall384).count() == 1


# ---------------------------------------------------------------------------
# Single-flight
# ---------------------------------------------------------------------------

def test_process_queue_is_single_flight_across_connections(db_session, fake_embedder, pg_url):
    """Two concurrent fastembed subprocesses take the 7.8 GB server to ~350 MB
    available and it thrashes to a standstill, with Postgres on the same box.
    Observed twice: the scheduled cron racing a manual drain.

    The old code claimed to be single-flight because the scheduler runs it with
    `max_instances=1` — a property of one APScheduler job, not of this function.
    """
    from sqlalchemy import create_engine, text

    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    db_session.commit()

    # A separate connection holds the lock, standing in for the other process.
    other = create_engine(pg_url)
    with other.connect() as conn:
        held = conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": emb._QUEUE_LOCK_KEY}
        ).scalar()
        assert held is True

        assert EmbeddingService.process_queue(db_session) == 0
        # The item is untouched, not consumed or half-processed.
        assert db_session.query(EmbeddingQueue).filter_by(status="pending").count() == 1

    other.dispose()

    # Lock released with that connection — normal service resumes.
    assert EmbeddingService.process_queue(db_session) == 1


def test_process_queue_releases_its_lock(db_session, fake_embedder, pg_url):
    from sqlalchemy import create_engine, text

    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    db_session.commit()
    EmbeddingService.process_queue(db_session)

    other = create_engine(pg_url)
    with other.connect() as conn:
        assert conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": emb._QUEUE_LOCK_KEY}
        ).scalar() is True
    other.dispose()


def test_lock_is_released_even_when_a_batch_raises(db_session, monkeypatch, pg_url):
    from sqlalchemy import create_engine, text

    def boom(_texts):
        raise MemoryError("simulating the actual failure mode")

    monkeypatch.setattr(emb, "_embed_via_subprocess", boom)
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    db_session.commit()

    EmbeddingService.process_queue(db_session)  # swallowed by the bisect path

    other = create_engine(pg_url)
    with other.connect() as conn:
        assert conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": emb._QUEUE_LOCK_KEY}
        ).scalar() is True
    other.dispose()


# ---------------------------------------------------------------------------
# Provider config access
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,expected", [("a-real-key", True), (None, False), ("", False)])
def test_gemini_available_reads_the_typed_config_model(monkeypatch, key, expected):
    """`plugin_config()` returns a typed pydantic model, not a dict.

    The original code called `.get("gemini_api_key")` on it, which raises
    AttributeError — and a broad `except Exception: return False` turned that
    into a quiet "not configured". The bug's failure mode and the correct answer
    for an unconfigured deployment were identical, which is why it shipped.
    """
    from pydantic import BaseModel

    from app.plugin import embedding_provider as ep

    class _Cfg(BaseModel):
        gemini_api_key: str | None = None

    monkeypatch.setattr(ep, "plugin_config", lambda name: _Cfg(gemini_api_key=key),
                        raising=False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.plugin.config_store",
        type("m", (), {"plugin_config": staticmethod(lambda name: _Cfg(gemini_api_key=key))}),
    )
    assert ep.GeminiEmbeddingProvider().available() is expected


def test_gemini_available_is_false_without_a_database(monkeypatch):
    """Boot and unit-test contexts have no DB; that must read as unavailable
    rather than exploding — but ONLY database errors are swallowed."""
    from sqlalchemy.exc import OperationalError

    def boom(_name):
        raise OperationalError("select 1", {}, Exception("no db"))

    monkeypatch.setitem(
        __import__("sys").modules,
        "app.plugin.config_store",
        type("m", (), {"plugin_config": staticmethod(boom)}),
    )
    from app.plugin.embedding_provider import GeminiEmbeddingProvider

    assert GeminiEmbeddingProvider().available() is False


# ---------------------------------------------------------------------------
# Retry classification
# ---------------------------------------------------------------------------

class _ApiError(Exception):
    """Shape of google.genai's error: carries an HTTP `code`."""

    def __init__(self, code, message="boom"):
        super().__init__(message)
        self.code = code


@pytest.mark.parametrize("code,attempts", [
    (400, 1),   # INVALID_ARGUMENT — the request is wrong; asking again won't fix it
    (404, 1),   # wrong model name
    (429, 3),   # rate limited — the one client error that IS temporal
    (503, 3),   # server side
])
def test_gemini_retries_only_what_retrying_can_fix(monkeypatch, code, attempts):
    """A permanent 4xx spent two minutes of backoff reaching a foregone answer.

    Worse, it buried the cause under seven identical warnings: the real message
    ("contains an empty Part") appeared eight times and read like flakiness.
    """
    from app.plugin.embedding_provider import GeminiEmbeddingProvider

    p = GeminiEmbeddingProvider()
    p.max_retries = 3
    calls = []

    class _Models:
        def embed_content(self, **kw):
            calls.append(kw)
            raise _ApiError(code)

    monkeypatch.setattr(p, "_get_client",
                        lambda: type("C", (), {"models": _Models()})())
    monkeypatch.setattr(p._limiter, "acquire", lambda n: None)
    monkeypatch.setattr(p._limiter, "penalize", lambda s: None)

    with pytest.raises(RuntimeError):
        p._embed_batch(["hello"])

    assert len(calls) == attempts
