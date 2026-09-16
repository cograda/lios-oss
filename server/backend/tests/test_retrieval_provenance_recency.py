"""R4 (2026-09-04): retrieval provenance and recency decay (db tier for the
scoring/ranking behaviour against real pgvector; unit tier for the pure
helpers).

Exit check from the backlog item, literally: the 1 August `.stversions`
snapshot that outranked its live file at 0.7651 vs 0.7600 (see
`obsidian/sync.py`'s SKIP_DIRS comment) no longer does, in a test — modelled
here as two chunks with the same text, one `is_history` and 34 days older,
one live, where the snapshot has the *marginally better* raw cosine score
(exactly matching the shape of the real incident) yet the live chunk ranks
first once recency decay applies.

Mutation check included directly (not just described): with
`apply_recency_decay=False` on the same seeded data, the snapshot DOES win —
proving the ranking test above is actually exercising decay, not some other
effect (metadata parsing, source filter, etc.) that happens to reorder them.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import embedding as emb
from app.services.embedding import (
    Embedding,
    EmbeddingService,
    EmbeddingVecBgeSmall384,
)

# Only the DB-backed ranking tests below need real pgvector; the pure-helper
# classes at the bottom of this file (TestDecayFactor, TestChunkProvenance)
# are plain unit tests and deliberately not marked `db`, so they run in the
# fast tier too. Applied per-test rather than at module scope for that reason.


def _vec(x: float = 1.0) -> list[float]:
    v = [0.0] * emb.VECTOR_DIM
    v[0] = x
    v[1] = 1.0 - abs(x)
    return v


def _seed(session, source_id, vec, created_at, metadata: dict):
    import json

    row = Embedding(
        source="vault",
        source_id=source_id,
        user_id=None,
        chunk_text="identical content on both sides",
        content_hash=f"hash-{source_id}",
        metadata_json=json.dumps(metadata),
        created_at=created_at,
    )
    session.add(row)
    session.flush()
    session.add(EmbeddingVecBgeSmall384(
        embedding_id=row.id, embedding=vec, model_name=emb.MODEL_NAME,
    ))
    return row


@pytest.fixture
def snapshot_vs_live(db_session, monkeypatch):
    """The exact shape of the 1 Aug incident: a stale snapshot with a
    marginally BETTER raw cosine score than the live file it's a copy of."""
    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.array(_vec(1.0), dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())
    # Fixed, known recency settings rather than relying on the live manifest
    # default — this test is about the mechanism, not about what the default
    # half-life/staleness-threshold happen to be tuned to today. Threshold of
    # 30 (not the production default of 180) so the snapshot's 34-day age
    # actually crosses it and the `stale` label has something to demonstrate.
    monkeypatch.setattr(emb, "_recency_settings", lambda: (14.0, 30.0))

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=34)

    # Snapshot: identical to the query vector (best possible raw cosine),
    # 34 days old, flagged is_history.
    _seed(
        db_session, "Notes/plan.md.stversions/2026-08-01.md", _vec(1.0), old,
        {"source_date": old.isoformat(), "is_history": True},
    )
    # Live: marginally OFF the query vector (a slightly worse raw cosine),
    # current, not history.
    _seed(
        db_session, "Notes/plan.md", _vec(0.999), now,
        {"source_date": now.isoformat(), "is_history": False},
    )
    db_session.commit()
    return db_session


@pytest.mark.db
def test_live_file_outranks_its_own_stale_snapshot(snapshot_vs_live):
    results = EmbeddingService.search(snapshot_vs_live, "anything", sources=["vault"])
    assert [r["source_id"] for r in results][:2] == [
        "Notes/plan.md", "Notes/plan.md.stversions/2026-08-01.md",
    ]
    live, snap = results[0], results[1]
    assert live["is_history"] is False
    assert live["stale"] is False
    assert snap["is_history"] is True
    assert snap["stale"] is True
    # Sanity: the snapshot really did have the better RAW cosine score, so the
    # reordering below is decay's doing, not a tie broken some other way.
    assert snap["score"] < live["score"]


@pytest.mark.db
def test_mutation_check_snapshot_wins_without_decay(snapshot_vs_live):
    """Reinstating the bug: with decay disabled, the snapshot's better raw
    cosine score wins, exactly as it did in production on 2026-08-13. This is
    what proves the test above is testing recency decay and not some
    unrelated ordering effect."""
    results = EmbeddingService.search(
        snapshot_vs_live, "anything", sources=["vault"], apply_recency_decay=False,
    )
    assert results[0]["source_id"] == "Notes/plan.md.stversions/2026-08-01.md"


@pytest.mark.db
def test_provenance_fields_present_even_with_decay_off(snapshot_vs_live):
    """Constraint: source_date/is_history/stale are provenance, always
    returned — apply_recency_decay only controls whether they affect score."""
    results = EmbeddingService.search(
        snapshot_vs_live, "anything", sources=["vault"], apply_recency_decay=False,
    )
    for r in results:
        assert "source_date" in r
        assert "is_history" in r
        assert "stale" in r


# ---------------------------------------------------------------------------
# Pure-helper unit tests (no DB)
# ---------------------------------------------------------------------------


class TestDecayFactor:
    def test_no_age_no_decay(self):
        assert emb._decay_factor(0.0, 30.0) == 1.0

    def test_negative_age_clamped_to_no_decay(self):
        """A future source_date (clock skew) must never boost a score."""
        assert emb._decay_factor(-5.0, 30.0) == 1.0

    def test_halves_at_one_half_life(self):
        assert emb._decay_factor(30.0, 30.0) == pytest.approx(0.5)

    def test_quarters_at_two_half_lives(self):
        assert emb._decay_factor(60.0, 30.0) == pytest.approx(0.25)


class TestChunkProvenance:
    def test_falls_back_to_created_at_when_no_source_date(self):
        created = datetime.now(timezone.utc) - timedelta(days=5)
        prov = emb._chunk_provenance(None, created, 180.0)
        assert prov["source_date"] == created.isoformat()
        assert prov["is_history"] is False
        assert prov["stale"] is False

    def test_reads_explicit_source_date_and_is_history(self):
        import json

        old = datetime.now(timezone.utc) - timedelta(days=200)
        meta = json.dumps({"source_date": old.isoformat(), "is_history": True})
        prov = emb._chunk_provenance(meta, datetime.now(timezone.utc), 180.0)
        assert prov["is_history"] is True
        assert prov["stale"] is True
        assert prov["age_days"] == pytest.approx(200.0, abs=1.0)

    def test_malformed_metadata_degrades_to_created_at_fallback(self):
        created = datetime.now(timezone.utc)
        prov = emb._chunk_provenance("not json", created, 180.0)
        assert prov["source_date"] == created.isoformat()
        assert prov["is_history"] is False
