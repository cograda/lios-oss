"""Auto-dedup document ingestion by content hash (issue #148).

`ingest_path` hashes the raw source bytes before dispatching to a parser
and checks for an existing document with the same hash *and* the same
owner scope (`_find_duplicate`). A match short-circuits: no parsing, no
new document, no embedding — just the existing document returned with
`deduplicated: True`.

Three behaviours pinned here:
  - identical bytes under a new filename dedups against the existing doc
  - the same bytes for a *different* owner never dedups — ownership scope
    is exactly as strict as it is for search (test_corpus_ownership.py)
  - different bytes always ingests as its own document
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.integrations.historical_corpus.ingest import ingest_path
from app.integrations.historical_corpus.models import (
    HistoricalDocument, HistoricalDocumentChunk,
)
from app.services.embedding import EmbeddingQueue

pytestmark = pytest.mark.db

BODY = "# Memo 1 · A note\n\n**Summary:** something worth keeping.\n"
OTHER_BODY = "# Memo 2 · A different note\n\n**Summary:** not the same thing.\n"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_identical_bytes_under_a_new_name_dedups(db_session, tmp_path):
    first = _write(tmp_path, "original.md", BODY)
    result1 = ingest_path(db_session, first, project_tags=["t"])
    assert result1["created"] is True
    assert result1["deduplicated"] is False
    assert result1["chunks"] > 0
    assert result1["embeddings_enqueued"] > 0

    second = _write(tmp_path, "renamed-copy.md", BODY)
    result2 = ingest_path(db_session, second, project_tags=["t"])

    assert result2["deduplicated"] is True
    assert result2["existing_id"] == result1["document_id"]
    assert result2["chunks"] == 0
    assert result2["embeddings_enqueued"] == 0

    # No second document, no second round of chunks/embeddings.
    assert db_session.query(HistoricalDocument).filter_by(
        raw_content_hash=db_session.query(HistoricalDocument.raw_content_hash)
        .filter_by(id=result1["document_id"]).scalar(),
    ).count() == 1
    assert db_session.query(HistoricalDocumentChunk).filter_by(
        document_id=result1["document_id"],
    ).count() == result1["chunks"]

    # The new filename is recorded on the existing document's metadata.
    doc = db_session.get(HistoricalDocument, result1["document_id"])
    assert "renamed-copy.md" in doc.doc_metadata.get("alternate_sources", [])


def test_same_bytes_for_a_different_owner_does_not_dedup(db_session, tmp_path):
    alex_file = _write(tmp_path, "alexs-copy.md", BODY)
    result1 = ingest_path(db_session, alex_file, project_tags=["t"], owner_user_id=1)
    assert result1["created"] is True

    sam_file = _write(tmp_path, "sams-copy.md", BODY)
    result2 = ingest_path(db_session, sam_file, project_tags=["t"], owner_user_id=2)

    assert result2.get("deduplicated") is False
    assert result2["created"] is True
    assert result2["document_id"] != result1["document_id"]

    docs = db_session.query(HistoricalDocument).filter_by(
        raw_content_hash=db_session.get(HistoricalDocument, result1["document_id"]).raw_content_hash,
    ).all()
    assert {d.id for d in docs} == {result1["document_id"], result2["document_id"]}
    assert {d.owner_user_id for d in docs} == {1, 2}


def test_household_shared_does_not_dedup_against_an_owned_copy(db_session, tmp_path):
    """NULL-owner (household) only dedups against another NULL-owner doc —
    never against a private one, even with identical bytes."""
    owned = _write(tmp_path, "owned.md", BODY)
    result1 = ingest_path(db_session, owned, project_tags=["t"], owner_user_id=1)

    shared = _write(tmp_path, "shared.md", BODY)
    result2 = ingest_path(db_session, shared, project_tags=["t"])  # owner_user_id=None

    assert result2["deduplicated"] is False
    assert result2["created"] is True
    assert result2["document_id"] != result1["document_id"]


def test_different_bytes_ingests_as_its_own_document(db_session, tmp_path):
    first = _write(tmp_path, "a.md", BODY)
    result1 = ingest_path(db_session, first, project_tags=["t"])

    second = _write(tmp_path, "b.md", OTHER_BODY)
    result2 = ingest_path(db_session, second, project_tags=["t"])

    assert result2["deduplicated"] is False
    assert result2["created"] is True
    assert result2["document_id"] != result1["document_id"]
    doc1 = db_session.get(HistoricalDocument, result1["document_id"])
    doc2 = db_session.get(HistoricalDocument, result2["document_id"])
    assert doc1.raw_content_hash != doc2.raw_content_hash


def test_reingesting_the_same_path_unchanged_still_skips_via_existing_mechanism(db_session, tmp_path):
    """Same path, same bytes, re-run: this is the pre-existing content_hash
    skip (parsed-text, keyed on source_path), not the new raw-hash dedup —
    both must still coexist without double-counting."""
    p = _write(tmp_path, "same.md", BODY)
    result1 = ingest_path(db_session, p, project_tags=["t"])
    result2 = ingest_path(db_session, p, project_tags=["t"])

    assert result2["document_id"] == result1["document_id"]
    assert result2["created"] is False
    # Not reported as a raw-hash dedup — it's the same source_path, handled
    # by _upsert_document's own unchanged-content branch.
    assert result2.get("deduplicated") is False
    assert result2["embeddings_enqueued"] == 0

    queued = db_session.query(EmbeddingQueue).filter_by(source="historical_corpus").count()
    assert queued == result1["embeddings_enqueued"]
