"""Historical corpus ownership — a private document inside a shared archive.

Alex's decision of 2026-09-06: his claude.ai conversation history is his,
not the household's. Before this, `historical_documents` had no owner
column, every corpus chunk was embedded with `embeddings.user_id NULL`
(household-shared), and `claude_history_search` answered ANY caller from his
5,000 conversations.

The mechanism under test has two locks:

  1. The embedding rows of an owned document carry `user_id = owner`, so
     `EmbeddingService.search`'s existing `user_id IS NULL OR user_id =
     caller` clause excludes them for everyone else — no corpus-specific
     query logic. That is what the search-level tests exercise.
  2. `tools._enrich_hits` re-checks `owner_user_id` on the hydrated document,
     so a hit that reached it by any other route (a stale NULL embedding
     row) is still refused. `test_enrich_refuses_an_owned_document_to_a_non_
     owner` exercises this lock in isolation — the search tests cannot,
     because lock 1 hides the leak before lock 2 is reached.

Every other source type stays household-shared: a manual is visible to both
users, by design.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.auth.context import _current_user_id, use_user
from app.integrations.historical_corpus import tools as corpus_tools
from app.integrations.historical_corpus.ingest import EMBEDDING_SOURCE, _upsert_document
from app.integrations.historical_corpus.models import (
    HistoricalDocument, HistoricalDocumentChunk,
)
from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta
from app.services import embedding as emb
from app.services.embedding import Embedding, EmbeddingQueue, EmbeddingVecBgeSmall384

pytestmark = pytest.mark.db

ALEX, SAM = 1, 2


def _vec(x: float) -> list[float]:
    v = [0.0] * emb.VECTOR_DIM
    v[0] = x
    v[1] = 1.0 - abs(x)
    return v


def _seed_document(
    session, *, source_type: str, title: str, owner_user_id: int | None,
    embedding_user_id: int | None, similarity: float = 0.9,
):
    """One document with one chunk, embedded in the local space.

    `owner_user_id` is the document column; `embedding_user_id` is what the
    chunk's embedding row carries. They are separate arguments on purpose so
    a test can seed the half-migrated state (owned document, NULL embedding)
    that lock 2 exists for.
    """
    doc = HistoricalDocument(
        source_path=f"seed/{source_type}/{title}",
        source_type=source_type,
        title=title,
        content_hash=f"doc-{source_type}-{title}",
        owner_user_id=owner_user_id,
    )
    session.add(doc)
    session.flush()
    text_ = f"{title} chunk 0"
    session.add(HistoricalDocumentChunk(
        document_id=doc.id, chunk_index=0, chunk_type="text",
        chunk_text=text_, content_hash=f"{doc.id}:0",
    ))
    row = Embedding(
        source=EMBEDDING_SOURCE, source_id=f"{doc.id}:0", user_id=embedding_user_id,
        chunk_text=text_, content_hash=f"{doc.id}:0",
    )
    session.add(row)
    session.flush()
    session.add(EmbeddingVecBgeSmall384(
        embedding_id=row.id, embedding=_vec(similarity), model_name=emb.MODEL_NAME,
    ))
    session.flush()
    return doc


@pytest.fixture
def corpus(db_session, monkeypatch):
    """Alex's private conversation + a household manual, both embedded."""
    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.array(_vec(1.0), dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())

    private = _seed_document(
        db_session, source_type="claude_conversation", title="Alex asks about heat pumps",
        owner_user_id=ALEX, embedding_user_id=ALEX, similarity=0.99,
    )
    shared = _seed_document(
        db_session, source_type="manual", title="Neff dishwasher manual",
        owner_user_id=None, embedding_user_id=None, similarity=0.9,
    )
    db_session.commit()
    return db_session, private, shared


def _titles(payload: str) -> list[str]:
    return [r["title"] for r in json.loads(payload)["results"]]


# ---------------------------------------------------------------------------
# Search — as the non-owner, the owner, and unbound
# ---------------------------------------------------------------------------


def test_claude_history_search_returns_nothing_of_alexs_to_sam(corpus):
    session, private, _ = corpus
    with use_user(SAM):
        out = json.loads(corpus_tools.claude_history_search_handler(
            session, {"query": "heat pumps", "limit": 10},
        ))
    assert out["count"] == 0, out
    assert private.title not in json.dumps(out)


def test_corpus_search_by_type_returns_nothing_of_alexs_to_sam(corpus):
    session, private, _ = corpus
    with use_user(SAM):
        out = corpus_tools.corpus_search_handler(
            session, {"query": "heat pumps", "limit": 10,
                      "source_types": ["claude_conversation"]},
        )
    assert _titles(out) == []
    assert private.title not in out


def test_unfiltered_corpus_search_as_sam_sees_only_the_shared_manual(corpus):
    session, private, shared = corpus
    with use_user(SAM):
        out = corpus_tools.corpus_search_handler(session, {"query": "anything", "limit": 10})
    assert _titles(out) == [shared.title]


def test_alex_still_finds_his_own_history(corpus):
    session, private, _ = corpus
    with use_user(ALEX):
        history = corpus_tools.claude_history_search_handler(
            session, {"query": "heat pumps", "limit": 10},
        )
        typed = corpus_tools.corpus_search_handler(
            session, {"query": "heat pumps", "limit": 10,
                      "source_types": ["claude_conversation"]},
        )
    assert _titles(history) == [private.title]
    assert _titles(typed) == [private.title]


def test_a_shared_manual_is_visible_to_both(corpus):
    session, _, shared = corpus
    for uid in (ALEX, SAM):
        with use_user(uid):
            out = corpus_tools.corpus_search_handler(
                session, {"query": "dishwasher", "limit": 10, "source_types": ["manual"]},
            )
        assert _titles(out) == [shared.title], f"user {uid}"


def test_unbound_caller_sees_shared_only(corpus):
    """A background job (no bound user) gets the household archive, never a
    person's private history — same rule EmbeddingService.search applies."""
    session, private, shared = corpus
    token = _current_user_id.set(0)
    try:
        out = corpus_tools.corpus_search_handler(session, {"query": "anything", "limit": 10})
    finally:
        _current_user_id.reset(token)
    assert _titles(out) == [shared.title]


# ---------------------------------------------------------------------------
# Lock 2 — the document-level re-check, in isolation
# ---------------------------------------------------------------------------


def test_enrich_refuses_an_owned_document_to_a_non_owner(db_session):
    """Seed the half-migrated state: owned document, but its embedding row
    still NULL (shared). The vector search WOULD return that hit to Sam;
    `_enrich_hits` must still drop it. This is the only test that reaches
    the second lock, so it is the one that fails if that lock is removed."""
    doc = _seed_document(
        db_session, source_type="claude_conversation", title="Stale NULL embedding",
        owner_user_id=ALEX, embedding_user_id=None,
    )
    db_session.commit()
    hit = [{"source_id": f"{doc.id}:0", "score": 0.9}]

    with use_user(SAM):
        assert corpus_tools._enrich_hits(db_session, hit) == []
    with use_user(ALEX):
        assert [r["title"] for r in corpus_tools._enrich_hits(db_session, hit)] == [doc.title]


def test_corpus_stats_counts_only_what_the_caller_can_search(corpus):
    session, _, _ = corpus
    with use_user(SAM):
        sam = json.loads(corpus_tools.corpus_stats_handler(session, {}))
    with use_user(ALEX):
        alex = json.loads(corpus_tools.corpus_stats_handler(session, {}))
    assert {r["source_type"] for r in sam["by_source_type"]} == {"manual"}
    assert {r["source_type"] for r in alex["by_source_type"]} == {"manual", "claude_conversation"}
    assert sam["total_documents"] == 1
    assert alex["total_documents"] == 2


# ---------------------------------------------------------------------------
# Ingest — the owner is stamped on the document AND threaded to the queue
# ---------------------------------------------------------------------------


def _meta(source_type: str) -> DocMeta:
    return DocMeta(title="t", source_type=source_type, author=None, participants=None,
                   document_date=None, metadata={})


def test_upsert_stamps_owner_on_document_and_queue(db_session):
    doc, created, enq = _upsert_document(
        db_session, source_path="claude_export#conversation=abc", meta=_meta("claude_conversation"),
        chunks=[ChunkRecord(chunk_type="turn", chunk_text="private words", breadcrumb=None, metadata={})],
        project_tags=["claude-conversations"], owner_user_id=ALEX,
    )
    assert created and enq == 1
    assert doc.owner_user_id == ALEX
    queued = db_session.query(EmbeddingQueue).filter_by(
        source=EMBEDDING_SOURCE, source_id=f"{doc.id}:0",
    ).one()
    assert queued.user_id == ALEX, "the queue row is what becomes the embedding's user_id"


def test_upsert_defaults_to_household_shared(db_session):
    doc, _, _ = _upsert_document(
        db_session, source_path="manuals/neff.md", meta=_meta("manual"),
        chunks=[ChunkRecord(chunk_type="section", chunk_text="rinse aid", breadcrumb=None, metadata={})],
        project_tags=["manuals"],
    )
    assert doc.owner_user_id is None
    queued = db_session.query(EmbeddingQueue).filter_by(
        source=EMBEDDING_SOURCE, source_id=f"{doc.id}:0",
    ).one()
    assert queued.user_id is None


def test_claude_export_ingest_requires_an_owner():
    """There is no shared Claude export, so there is no default owner."""
    from app.integrations.historical_corpus.ingest import ingest_claude_export

    with pytest.raises((TypeError, ValueError)):
        ingest_claude_export(None, [])  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Migration data fix — the exact SQL, run against a seeded database
# ---------------------------------------------------------------------------


def _migration_module():
    path = next(
        (Path(__file__).resolve().parent.parent / "alembic" / "versions")
        .glob("*_historical_documents_owner.py")
    )
    spec = importlib.util.spec_from_file_location("mig_hist_owner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_data_fix_moves_existing_conversations_to_alex(db_session):
    """Run the migration's own statements (imported from the file, not
    re-typed) on the pre-decision state: conversations with NULL owner and
    NULL-user embeddings, next to a manual that must stay shared."""
    mig = _migration_module()
    convs = [
        _seed_document(db_session, source_type="claude_conversation", title=f"conv {i}",
                       owner_user_id=None, embedding_user_id=None)
        for i in range(3)
    ]
    manual = _seed_document(db_session, source_type="manual", title="manual",
                            owner_user_id=None, embedding_user_id=None)
    # A pending queue row for one conversation, as a re-ingest mid-flight would leave.
    db_session.add(EmbeddingQueue(
        source=EMBEDDING_SOURCE, source_id=f"{convs[0].id}:1", user_id=None,
        content="pending", content_hash="p1", status="pending",
    ))
    # A non-corpus embedding whose source_id has no ':' — must be untouched.
    other = Embedding(source="vault", source_id="Notes/x.md", user_id=None,
                      chunk_text="x", content_hash="x")
    db_session.add(other)
    db_session.commit()

    touched = [db_session.execute(text(sql)).rowcount for sql in mig.DATA_FIX_SQL]
    db_session.commit()

    assert touched == [3, 3, 1], f"(documents, embeddings, queue) rows updated: {touched}"
    db_session.expire_all()
    assert all(c.owner_user_id == mig.CLAUDE_HISTORY_OWNER_ID == ALEX for c in convs)
    assert manual.owner_user_id is None
    emb_users = {
        r.source_id: r.user_id
        for r in db_session.query(Embedding).filter_by(source=EMBEDDING_SOURCE).all()
    }
    assert emb_users == {**{f"{c.id}:0": ALEX for c in convs}, f"{manual.id}:0": None}
    assert db_session.query(EmbeddingQueue).one().user_id == ALEX
    assert db_session.query(Embedding).filter_by(source="vault").one().user_id is None

    # Idempotent: a second run touches nothing.
    assert [db_session.execute(text(sql)).rowcount for sql in mig.DATA_FIX_SQL] == [0, 0, 0]


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_claude_history_search_describes_itself_as_yours_not_alexs():
    tool = next(t for t in corpus_tools.mcp_tools() if t["name"] == "claude_history_search")
    assert "Alex" not in tool["description"]
    assert "your" in tool["description"]
