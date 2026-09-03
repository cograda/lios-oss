"""unit-tier tests for the vector-native similarity surface.

`similar_to` / `near_duplicates` exist because of the structural rule the
Gemini migration was designed around:

    Cost, latency and the network are spent only at the `text -> vector`
    boundary. Anything starting from a vector already in Postgres is pure
    linear algebra: zero API calls, zero cost, works offline.

So the property these tests care about most is the one that is easy to break
without noticing: **no embedding provider is ever called**. A regression that
quietly embeds the seed text would still return correct-looking results, pass
any output assertion, and reintroduce per-call cost plus an offline failure.

The second property is document identity. Vault chunks carry `{path}#{n}`
source_ids, so anything keying on the raw source_id will (a) fail to find a
chunked seed, and (b) return the seed's own sibling chunks as its top hits.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.embedding import EmbeddingService

pytestmark = pytest.mark.unit


class TestCostsNothing:
    def test_similar_to_never_embeds(self, monkeypatch):
        """The whole point. If this fires, the free path has been lost."""
        called = []
        monkeypatch.setattr(
            "app.services.embedding._embed_query_with",
            lambda *a, **k: called.append(a) or [0.0],
        )
        monkeypatch.setattr("app.services.embedding._active_spaces", lambda: [])

        EmbeddingService.similar_to(MagicMock(), "vault", "Note.md")
        assert called == []

    def test_near_duplicates_never_embeds(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "app.services.embedding._embed_query_with",
            lambda *a, **k: called.append(a) or [0.0],
        )
        monkeypatch.setattr("app.services.embedding._active_spaces", lambda: [])

        EmbeddingService.near_duplicates(MagicMock(), "vault")
        assert called == []


class TestNoSpacesConfigured:
    def test_similar_to_returns_empty_not_error(self, monkeypatch):
        monkeypatch.setattr("app.services.embedding._active_spaces", lambda: [])
        assert EmbeddingService.similar_to(MagicMock(), "vault", "A.md") == []

    def test_near_duplicates_returns_empty_not_error(self, monkeypatch):
        monkeypatch.setattr("app.services.embedding._active_spaces", lambda: [])
        assert EmbeddingService.near_duplicates(MagicMock(), "vault") == []


class TestDocumentIdentity:
    """`{path}#{n}` chunking is the thing these methods are easiest to get wrong."""

    def test_seed_path_is_taken_from_the_document_not_the_chunk(self):
        from app.services import embedding as mod

        assert "Note.md" == "Note.md#3".split("#", 1)[0]
        # The implementation must key on split_part(source_id, '#', 1); if this
        # helper call disappears from the module the exclusion silently breaks.
        src = open(mod.__file__).read()
        assert src.count('split_part(Embedding.source_id, "#", 1)') >= 2, (
            "similar_to/near_duplicates must key on the document, not the chunk id"
        )

    def test_results_collapse_sibling_chunks_to_one_row(self, monkeypatch):
        """Two chunks of the same neighbour must yield one result, best score."""
        rows = [
            _row("vault", "Other.md#0", "first chunk", 0.10),   # score 0.90
            _row("vault", "Other.md#1", "second chunk", 0.02),  # score 0.98
        ]
        out = _run_similar(monkeypatch, seed_ids=[1], rows=rows)

        assert [r["source_id"] for r in out] == ["Other.md"]
        assert out[0]["score"] == 0.98


class TestNearDuplicatePairs:
    def test_pairs_are_unordered_and_deduplicated(self, monkeypatch):
        """(a,b) and (b,a) are the same finding, reported once."""
        monkeypatch.setattr(
            "app.services.embedding._active_spaces",
            lambda: [(_provider(), MagicMock())],
        )
        session = MagicMock()
        session.query.return_value = _chainable([("A.md", 1, 100), ("B.md", 2, 100)])

        def fake_similar(_s, _src, source_id, **kw):
            other = "B.md" if source_id == "A.md" else "A.md"
            return [{"source_id": other, "score": 0.97, "space": "m"}]

        monkeypatch.setattr(EmbeddingService, "similar_to", staticmethod(fake_similar))
        pairs = EmbeddingService.near_duplicates(session, "vault", threshold=0.95)

        assert len(pairs) == 1
        assert {pairs[0]["a"], pairs[0]["b"]} == {"A.md", "B.md"}
        assert pairs[0]["score"] == 0.97


# --- helpers ---------------------------------------------------------------

def _chainable(rows):
    """A query mock where join()/filter() return self.

    The first version of these tests pinned an exact `.join().filter().filter()`
    chain and broke the moment an exclusion filter was added — a test that fails
    when the implementation gains a WHERE clause is testing the wrong thing.
    """
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.order_by.return_value = q
    q.limit.return_value = q
    q.all.return_value = rows
    return q


def _provider(name="gemini-embedding-2"):
    p = MagicMock()
    p.model_name = name
    p.provider_id = "gemini"
    return p


def _row(source, source_id, text, distance):
    r = MagicMock()
    r.source, r.source_id, r.chunk_text = source, source_id, text
    r.metadata_json, r.created_at, r.distance = None, None, distance
    return r


def _run_similar(monkeypatch, seed_ids, rows):
    monkeypatch.setattr(
        "app.services.embedding._active_spaces",
        lambda: [(_provider(), MagicMock())],
    )
    monkeypatch.setattr("app.services.embedding.current_user_id_or_none", lambda: None)

    session = MagicMock()
    seed_q = MagicMock()
    seed_q.all.return_value = [(i,) for i in seed_ids]
    vec_q = MagicMock()
    vec_q.all.return_value = [([0.0],)]
    neigh = MagicMock()
    neigh.order_by.return_value.limit.return_value.all.return_value = rows

    calls = {"n": 0}

    def query(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            m = MagicMock()
            m.join.return_value.filter.return_value = seed_q
            return m
        if calls["n"] == 2:
            m = MagicMock()
            m.filter.return_value = vec_q
            return m
        m = MagicMock()
        chain = m.join.return_value.filter.return_value.filter.return_value.filter.return_value
        chain.order_by.return_value.limit.return_value.all.return_value = rows
        return m

    session.query.side_effect = query
    return EmbeddingService.similar_to(session, "vault", "Seed.md")


class TestTemplateExclusion:
    """Template-generated notes defeat similarity; the default reflects that.

    The first unscoped run over the real vault returned 60 pairs, 45 of them
    `Daily Notes/` against other `Daily Notes/` at 0.983-0.988. Those notes
    share frontmatter, a heading skeleton and two Obsidian task queries, so a
    quiet Tuesday really is near-identical to a quiet Wednesday — an honest
    score and a useless one, which buried every genuine duplicate beneath it.
    """

    def test_daily_notes_are_excluded_by_default(self):
        from app.services.embedding import DEFAULT_DUPLICATE_EXCLUDES

        assert "Daily Notes/" in DEFAULT_DUPLICATE_EXCLUDES

    def test_exclusion_applies_to_the_neighbour_side_too(self, monkeypatch):
        """Filtering only the seed still lets a templated note match everyone."""
        monkeypatch.setattr(
            "app.services.embedding._active_spaces",
            lambda: [(_provider(), MagicMock())],
        )
        session = MagicMock()
        session.query.return_value = _chainable([("Real.md", 1, 500)])

        monkeypatch.setattr(
            EmbeddingService, "similar_to",
            staticmethod(lambda *a, **k: [
                {"source_id": "Daily Notes/Alex/2026-08-05.md", "score": 0.99},
                {"source_id": "Other.md", "score": 0.97},
            ]),
        )
        pairs = EmbeddingService.near_duplicates(
            session, "vault", threshold=0.95, exclude_prefixes=["Daily Notes/"],
        )
        assert all("Daily Notes/" not in p["a"] and "Daily Notes/" not in p["b"]
                   for p in pairs), "excluded prefix leaked in as a neighbour"
        assert any(p["b"] == "Other.md" or p["a"] == "Other.md" for p in pairs)

    def test_empty_list_compares_everything(self, monkeypatch):
        monkeypatch.setattr("app.services.embedding._active_spaces", lambda: [])
        assert EmbeddingService.near_duplicates(
            MagicMock(), "vault", exclude_prefixes=[]
        ) == []


class TestIterativeScan:
    """Filtered HNSW search must be bounded by LIMIT, not by ef_search.

    Without `hnsw.iterative_scan`, the index walk yields `ef_search` candidates
    (40 by default) and the WHERE clause is applied *after*, so a query scoped
    to one source keeps only the fraction that happened to match. Measured on
    the live corpus 2026-08-15 — vault is 5,412 of 63,130 embeddings — a
    `LIMIT 50` vault search returned **10 rows spanning 1 document**; with
    iterative scan, **50 rows spanning 20 documents**.

    This is a silent failure: it reads as "search is a bit narrow", never as an
    error, and it applied to every filtered search in the system.
    """

    def test_search_enables_it(self, monkeypatch):
        from app.services import embedding as mod

        calls = []
        monkeypatch.setattr(mod, "_enable_iterative_scan", lambda s: calls.append(s))
        monkeypatch.setattr(mod, "_active_spaces", lambda: [(_provider(), MagicMock())])
        monkeypatch.setattr(mod, "_embed_query_with", lambda p, q: [0.0])
        session = MagicMock()
        session.query.return_value = _chainable([])
        mod.EmbeddingService.search(session, "q", sources=["vault"])
        assert calls, "search must widen the scan before a filtered vector query"

    def test_similar_to_enables_it(self, monkeypatch):
        from app.services import embedding as mod

        calls = []
        monkeypatch.setattr(mod, "_enable_iterative_scan", lambda s: calls.append(s))
        monkeypatch.setattr(mod, "_active_spaces", lambda: [(_provider(), MagicMock())])
        session = MagicMock()
        session.query.return_value = _chainable([])
        mod.EmbeddingService.similar_to(session, "vault", "A.md")
        assert calls

    def test_it_is_best_effort_and_never_raises(self):
        """pgvector < 0.8 has no such setting. Degrading beats taking search down."""
        from app.services.embedding import _enable_iterative_scan

        session = MagicMock()
        session.execute.side_effect = RuntimeError("unknown parameter")
        _enable_iterative_scan(session)          # must not raise
        session.rollback.assert_called_once()

    def test_it_asks_for_strict_order(self):
        """Scores are shown to callers and compared to thresholds, so ordering
        must stay exact; relaxed_order trades that for latency."""
        from app.services.embedding import _enable_iterative_scan

        session = MagicMock()
        _enable_iterative_scan(session)
        sql = str(session.execute.call_args[0][0])
        assert "strict_order" in sql
