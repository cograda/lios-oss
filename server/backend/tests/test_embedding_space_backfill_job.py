"""db tier — the nightly `embedding_space_backfill` cron job.

`run_embedding_processor` (the `*/5` cron) now writes only the primary
embedding space (`process_queue`'s `spaces="primary"` default,
2026-09-07). This job is the other half: fill every other active space's
gap overnight, reusing `backfill.fill_space` — the same walker the
`fill-space` CLI command already used to turn a new provider on.
"""

from __future__ import annotations

import pytest

from app.integrations.embedding import tasks
from app.integrations.embedding.models import (
    EmbeddingVecBgeSmall384,
    EmbeddingVecGemini1536,
)
from app.plugin.config_store import set_config_value
from app.services import embedding as emb
from app.services.embedding import EmbeddingService

pytestmark = pytest.mark.db


def _vec(x: float = 1.0) -> list[float]:
    v = [0.0] * emb.VECTOR_DIM
    v[0] = x
    return v


@pytest.fixture
def fake_embedder(monkeypatch):
    """Deterministic stand-in for the fastembed subprocess (local space)."""

    def embed(texts):
        return [_vec() for _ in texts]

    monkeypatch.setattr(emb, "_embed_via_subprocess", embed)
    return embed


class _FakeRemoteProvider:
    """Primary space stand-in — same shape as test_embedding_pipeline.py's,
    duplicated locally to keep this file self-contained."""

    provider_id = "gemini-embedding-2"
    model_name = "gemini-embedding-2"
    dim = 1536

    def available(self) -> bool:
        return True

    def embed(self, texts):
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


@pytest.fixture
def two_spaces_primary_gemini(monkeypatch, fake_embedder):
    """Gemini primary (first), local fastembed second — matches production
    ordering (`embedding_provider` config lists the search default first)."""
    from app.plugin.embedding_provider import FastEmbedProvider

    monkeypatch.setattr(emb, "_active_spaces", lambda: [
        (_FakeRemoteProvider(), EmbeddingVecGemini1536),
        (FastEmbedProvider(), EmbeddingVecBgeSmall384),
    ])
    # fill_space looks providers up by id in the real registry, not via
    # _active_spaces() — see backfill.fill_space's docstring/callers.
    monkeypatch.setitem(
        __import__(
            "app.plugin.embedding_provider", fromlist=["_PROVIDERS"]
        )._PROVIDERS,
        "gemini-embedding-2",
        _FakeRemoteProvider,
    )


def test_nightly_job_fills_only_the_missing_non_primary_rows(
    db_session, two_spaces_primary_gemini,
):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    # Default is "primary": only gemini gets written here.
    assert EmbeddingService.process_queue(db_session) == 1
    assert db_session.query(EmbeddingVecGemini1536).count() == 1
    assert db_session.query(EmbeddingVecBgeSmall384).count() == 0

    total, per_space, error = tasks._run_space_backfill_blocking()

    assert error is None
    assert total == 1
    assert per_space == {"fastembed-bge-small": 1}
    assert db_session.query(EmbeddingVecBgeSmall384).count() == 1
    # The primary space is untouched — this job only fills OTHER spaces.
    assert db_session.query(EmbeddingVecGemini1536).count() == 1


def test_nightly_job_is_a_clean_noop_when_nothing_is_missing(
    db_session, two_spaces_primary_gemini,
):
    total, per_space, error = tasks._run_space_backfill_blocking()
    assert total == 0
    assert error is None
    assert per_space == {"fastembed-bge-small": 0}


def test_nightly_job_stops_at_budget_without_filling_anything(
    db_session, two_spaces_primary_gemini,
):
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)
    assert db_session.query(EmbeddingVecBgeSmall384).count() == 0

    set_config_value("embedding", "space_backfill_budget_seconds", 0)

    total, per_space, error = tasks._run_space_backfill_blocking()

    assert error is None
    assert total == 0
    assert per_space == {}
    # Budget elapsed before any work — the gap is still there for tomorrow.
    assert db_session.query(EmbeddingVecBgeSmall384).count() == 0


def test_nightly_job_is_a_noop_with_zero_or_one_active_spaces(db_session, fake_embedder):
    """No "other space" exists to catch up when there's only one active
    space — the common case for a fresh deployment (local-only)."""
    EmbeddingService.enqueue(db_session, "vault", "a.md", "hello")
    EmbeddingService.process_queue(db_session)

    total, per_space, error = tasks._run_space_backfill_blocking()
    assert (total, per_space, error) == (0, {}, None)
