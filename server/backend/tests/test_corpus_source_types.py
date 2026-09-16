"""historical_corpus: `source_types` is a retrieval filter, not a display filter.

Two tiers in one file because they guard one contract:

  unit — the schema's list of valid source types is rendered from the code's
         registry (`parsers/types.py::KNOWN_SOURCE_TYPES`), and that registry
         equals what the producers actually write. The hand-typed prose it
         replaced omitted `manual`, `voice_memo` and `claude_conversation`.

  db   — the restriction is applied INSIDE the vector query. Seeds a wanted
         document whose only chunk ranks *below* more noise rows than the old
         post-filter's over-fetch (`min(limit*3, 50)`) could reach, and asserts
         the filtered search still returns it. Mutation-checked 2026-09-06:
         reinstating the post-filter in `_enrich_hits` and the over-fetch in
         `corpus_search_handler` makes `test_filtered_search_reaches_a_type_
         that_ranks_below_the_noise` fail with `count == 0` — the exact live
         symptom ("dishwasher filter cleaning and salt refill" with
         `source_types=["manual"]` → 0 results, the manual 5th unfiltered).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.historical_corpus import tools as corpus_tools
from app.integrations.historical_corpus.parsers.types import (
    KNOWN_SOURCE_TYPES,
    ATTACHMENT_SOURCE_PREFIXES,
    WHATSAPP_ATTACHMENT_KINDS,
)

# ---------------------------------------------------------------------------
# Unit tier — the registry and the schema agree, and both agree with the code
# ---------------------------------------------------------------------------

PARSERS_DIR = Path(corpus_tools.__file__).resolve().parent / "parsers"


def _source_type_literals_in(path: Path) -> set[str]:
    """Every `source_type="..."` keyword literal in a module, by AST.

    Literal-only on purpose: a parser that computes its type would be
    invisible here, and a registry test that cannot see a producer is a test
    that passes while the schema drifts. If a parser ever needs a computed
    type, register it explicitly and extend this scanner in the same commit.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "source_type"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                found.add(kw.value.value)
    return found


def _source_types_written_by_producers() -> set[str]:
    types: set[str] = set()
    for path in PARSERS_DIR.glob("*.py"):
        types |= _source_type_literals_in(path)
    # attachments/ingest.py is the one producer outside the parsers package.
    # It writes f"{SOURCE_TYPE_PREFIX[source]}_{kind}" for each kind in its
    # MIME table, so derive both axes from its tables rather than repeating
    # them here.
    from app.integrations.attachments.ingest import MIME_TO_PARSER, SOURCE_TYPE_PREFIX

    for prefix in SOURCE_TYPE_PREFIX.values():
        for kind, _parser in MIME_TO_PARSER.values():
            types.add(f"{prefix}_{kind}")
    return types


def test_known_source_types_equal_what_producers_write():
    assert set(KNOWN_SOURCE_TYPES) == _source_types_written_by_producers()
    # No duplicates: the schema renders this as an enum.
    assert len(KNOWN_SOURCE_TYPES) == len(set(KNOWN_SOURCE_TYPES))


def test_attachment_kinds_match_the_mime_table():
    from app.integrations.attachments.ingest import MIME_TO_PARSER

    assert set(WHATSAPP_ATTACHMENT_KINDS) == {k for k, _ in MIME_TO_PARSER.values()}


def test_attachment_prefixes_match_the_source_table():
    from app.integrations.attachments.ingest import SOURCE_TYPE_PREFIX
    from app.integrations.attachments.sources import SUPPORTED_INGEST_SOURCES

    assert set(ATTACHMENT_SOURCE_PREFIXES) == set(SOURCE_TYPE_PREFIX.values())
    # Every source ingest accepts must know what to call its documents.
    assert set(SOURCE_TYPE_PREFIX) == set(SUPPORTED_INGEST_SOURCES)


def _corpus_search_schema() -> dict:
    (tool,) = [t for t in corpus_tools.mcp_tools() if t["name"] == "corpus_search"]
    return tool["inputSchema"]["properties"]["source_types"]


def test_schema_enum_is_the_registry():
    schema = _corpus_search_schema()
    assert schema["items"]["enum"] == list(KNOWN_SOURCE_TYPES)


def test_schema_description_names_every_known_type():
    """The prose the model reads must list every type — that is what drifted."""
    description = _corpus_search_schema()["description"]
    for source_type in KNOWN_SOURCE_TYPES:
        assert source_type in description, source_type
    for previously_missing in ("manual", "voice_memo", "claude_conversation"):
        assert previously_missing in description


def test_unknown_source_type_is_a_loud_error_not_an_empty_result():
    session = MagicMock()
    with patch.object(corpus_tools.EmbeddingService, "search") as search:
        out = json.loads(corpus_tools.corpus_search_handler(
            session, {"query": "anything", "source_types": ["manuals"]},
        ))
    search.assert_not_called()
    assert "unknown source_types" in out["error"]
    assert "manuals" in out["error"]
    assert out["known_source_types"] == list(KNOWN_SOURCE_TYPES)


def test_filter_never_casts_a_non_corpus_source_id():
    """The semi-join builds the `doc:idx` key on the chunk side.

    A `split_part(embeddings.source_id, ':', 1)::int` predicate would be
    evaluated against every row the planner chooses to test — including
    vault `Note.md#3` ids — and Postgres does not promise WHERE-term order.
    """
    sql = str(corpus_tools.source_type_filter(["manual"]).compile(
        compile_kwargs={"literal_binds": True},
    ))
    assert "embeddings.source_id IN (SELECT" in sql
    assert "split_part" not in sql
    assert "historical_documents.source_type IN ('manual')" in sql


# ---------------------------------------------------------------------------
# DB tier — the restriction is inside the vector query
# ---------------------------------------------------------------------------

def _vec(x: float) -> list[float]:
    from app.services import embedding as emb

    v = [0.0] * emb.VECTOR_DIM
    v[0] = x
    v[1] = 1.0 - abs(x)
    return v


def _seed_document(session, *, source_type: str, title: str, similarities: list[float]):
    """One HistoricalDocument with one chunk per similarity, embedded in the
    local space with a vector at that cosine position relative to the query
    vector `_vec(1.0)`. Returns the document."""
    from app.integrations.historical_corpus.ingest import EMBEDDING_SOURCE
    from app.integrations.historical_corpus.models import (
        HistoricalDocument, HistoricalDocumentChunk,
    )
    from app.services import embedding as emb
    from app.services.embedding import Embedding, EmbeddingVecBgeSmall384

    doc = HistoricalDocument(
        source_path=f"seed/{source_type}/{title}",
        source_type=source_type,
        title=title,
        content_hash=f"doc-{source_type}-{title}",
    )
    session.add(doc)
    session.flush()
    for idx, sim in enumerate(similarities):
        text = f"{title} chunk {idx}"
        session.add(HistoricalDocumentChunk(
            document_id=doc.id, chunk_index=idx, chunk_type="text",
            chunk_text=text, content_hash=f"{doc.id}:{idx}",
        ))
        row = Embedding(
            source=EMBEDDING_SOURCE, source_id=f"{doc.id}:{idx}", user_id=None,
            chunk_text=text, content_hash=f"{doc.id}:{idx}",
        )
        session.add(row)
        session.flush()
        session.add(EmbeddingVecBgeSmall384(
            embedding_id=row.id, embedding=_vec(sim), model_name=emb.MODEL_NAME,
        ))
    session.flush()
    return doc


@pytest.fixture
def corpus_below_the_noise(db_session, monkeypatch):
    """Five WhatsApp chunks all closer to the query than the one manual chunk.

    With `limit=1` the old code over-fetched `min(1*3, 50) = 3` rows — all
    WhatsApp — and post-filtered them to nothing. The manual chunk sits at
    rank 6, past any over-fetch a limit of 1 would reach.
    """
    from app.services import embedding as emb

    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.array(_vec(1.0), dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())

    noise = _seed_document(
        db_session, source_type="whatsapp_txt", title="Kitchen group chat",
        similarities=[0.99, 0.98, 0.97, 0.96, 0.95],
    )
    wanted = _seed_document(
        db_session, source_type="manual", title="Neff dishwasher manual",
        similarities=[0.5],
    )
    db_session.commit()
    return db_session, noise, wanted


@pytest.mark.db
def test_filtered_search_reaches_a_type_that_ranks_below_the_noise(corpus_below_the_noise):
    session, _noise, wanted = corpus_below_the_noise

    out = json.loads(corpus_tools.corpus_search_handler(
        session, {"query": "dishwasher filter cleaning and salt refill",
                  "limit": 1, "source_types": ["manual"]},
    ))

    assert out["count"] == 1, out
    assert out["results"][0]["title"] == wanted.title
    assert out["results"][0]["source_type"] == "manual"


@pytest.mark.db
def test_filtered_search_returns_only_the_wanted_types(corpus_below_the_noise):
    session, _noise, _wanted = corpus_below_the_noise

    out = json.loads(corpus_tools.corpus_search_handler(
        session, {"query": "q", "limit": 10, "source_types": ["manual"]},
    ))
    assert {r["source_type"] for r in out["results"]} == {"manual"}

    out = json.loads(corpus_tools.corpus_search_handler(
        session, {"query": "q", "limit": 10, "source_types": ["whatsapp_txt"]},
    ))
    assert {r["source_type"] for r in out["results"]} == {"whatsapp_txt"}
    assert out["count"] == 5


@pytest.mark.db
def test_unfiltered_search_is_unchanged(corpus_below_the_noise):
    """No `source_types` → the exact single ORDER BY/LIMIT the raw search does:
    same rows, same scores, same order, and the low-ranked manual stays out."""
    from app.integrations.historical_corpus.ingest import EMBEDDING_SOURCE
    from app.services.embedding import EmbeddingService

    session, noise, _wanted = corpus_below_the_noise

    raw = EmbeddingService.search(
        session, "q", sources=[EMBEDDING_SOURCE], limit=3, apply_recency_decay=False,
    )
    out = json.loads(corpus_tools.corpus_search_handler(session, {"query": "q", "limit": 3}))

    assert [r["score"] for r in out["results"]] == [r["score"] for r in raw]
    assert [r["score"] for r in raw] == sorted((r["score"] for r in raw), reverse=True)
    assert all(r["title"] == noise.title for r in out["results"])
    assert all(r["source_type"] == "whatsapp_txt" for r in out["results"])


@pytest.mark.db
def test_claude_history_search_uses_the_same_retrieval_filter(db_session, monkeypatch):
    from app.services import embedding as emb

    class _FakeModel:
        def embed(self, texts):
            import numpy as np
            return [np.array(_vec(1.0), dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb, "get_model", lambda: _FakeModel())
    _seed_document(
        db_session, source_type="email_json", title="Builder thread",
        similarities=[0.99, 0.98, 0.97, 0.96],
    )
    _seed_document(
        db_session, source_type="claude_conversation", title="Old chat",
        similarities=[0.4],
    )
    db_session.commit()

    out = json.loads(corpus_tools.claude_history_search_handler(
        db_session, {"query": "q", "limit": 1},
    ))
    assert out["count"] == 1
    assert out["results"][0]["source_type"] == "claude_conversation"
