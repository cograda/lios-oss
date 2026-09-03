"""GeminiEmbeddingProvider — guards for the failure modes that are silent.

Every test here corresponds to a way this API misbehaves *without raising*,
verified against the live model in timemachine on 2026-08-03. A provider that
returns plausible-looking wrong vectors poisons the whole index and nothing
downstream would notice, so these are the tests that matter more than the
happy path.
"""

import sys
import types as pytypes

import pytest

from app.plugin.embedding_provider import GeminiEmbeddingProvider, get_provider


class _FakeEmbedding:
    def __init__(self, values):
        self.values = values


class _FakeResponse:
    def __init__(self, embeddings):
        self.embeddings = embeddings


class _FakeModels:
    """Records what it was asked for so tests can assert on request *shape*."""

    def __init__(self, dim=1536, n_override=None):
        self.dim = dim
        self.n_override = n_override
        self.calls = []

    def embed_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        n = self.n_override if self.n_override is not None else len(contents)
        return _FakeResponse([_FakeEmbedding([0.1] * self.dim) for _ in range(n)])


class _FakeClient:
    def __init__(self, models):
        self.models = models


@pytest.fixture
def genai_stub(monkeypatch):
    """Stub `google.genai` + `google.genai.types` in sys.modules.

    The SDK isn't a test dependency, and these tests are about how we *call* it,
    not about the SDK itself.
    """
    def _content(parts):
        return {"parts": parts}

    def _part(text):
        return {"text": text}

    types_mod = pytypes.ModuleType("google.genai.types")
    types_mod.Content = lambda parts: _content(parts)
    types_mod.Part = lambda text: _part(text)
    types_mod.EmbedContentConfig = lambda output_dimensionality: {
        "output_dimensionality": output_dimensionality
    }

    genai_mod = pytypes.ModuleType("google.genai")
    genai_mod.types = types_mod
    google_mod = pytypes.ModuleType("google")
    google_mod.genai = genai_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    return types_mod


def _provider(monkeypatch, models):
    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: {"gemini_api_key": "test-key"},
    )
    p = GeminiEmbeddingProvider()
    p._client = _FakeClient(models)  # skip real client construction
    return p


def test_each_text_becomes_its_own_content(genai_stub, monkeypatch):
    """The landmine: a plain list[str] returns ONE embedding for the whole batch.

    The SDK folds a list of strings into the parts of a single document and the
    API does not complain. So the request must carry one Content per text.
    """
    models = _FakeModels()
    p = _provider(monkeypatch, models)

    vecs = p.embed(["alpha", "beta", "gamma"])

    assert len(vecs) == 3
    contents = models.calls[0]["contents"]
    assert len(contents) == 3, "texts were folded into one Content — silent batching bug"
    assert [c["parts"][0]["text"] for c in contents] == ["alpha", "beta", "gamma"]


def test_short_result_list_raises_rather_than_mispairing(genai_stub, monkeypatch):
    """A short list zipped positionally against ids attaches every id to the
    wrong vector, and the tail silently goes unwritten. Must fail loudly."""
    models = _FakeModels(n_override=1)
    p = _provider(monkeypatch, models)

    with pytest.raises(RuntimeError, match="batching cannot be trusted"):
        p.embed(["a", "b", "c"])


def test_wrong_width_raises(genai_stub, monkeypatch):
    """3072-wide in a 1536 column is a hard failure; silently truncated is worse."""
    models = _FakeModels(dim=3072)
    p = _provider(monkeypatch, models)

    with pytest.raises(RuntimeError, match="returned 3072-dim"):
        p.embed(["a"])


def test_requests_the_configured_dimensionality(genai_stub, monkeypatch):
    models = _FakeModels()
    p = _provider(monkeypatch, models)
    p.embed(["a"])
    assert models.calls[0]["config"] == {"output_dimensionality": 1536}
    assert models.calls[0]["model"] == "gemini-embedding-2"


def test_query_is_instruction_prefixed_and_documents_are_not(genai_stub, monkeypatch):
    """gemini-embedding-2 dropped `task_type` for in-prompt task instructions.

    The corpus was embedded document-side, so a query embedded document-side
    lands in a different region of the space. The asymmetry is load-bearing.
    """
    models = _FakeModels()
    p = _provider(monkeypatch, models)

    p.embed(["renovation invoice"])
    doc_text = models.calls[0]["contents"][0]["parts"][0]["text"]
    assert doc_text == "renovation invoice"
    assert not doc_text.startswith("Task:")

    p.embed_query("renovation invoice")
    q_text = models.calls[1]["contents"][0]["parts"][0]["text"]
    assert q_text.startswith(GeminiEmbeddingProvider.query_instruction)
    assert q_text.endswith("renovation invoice")


def test_batches_are_chunked_at_batch_size(genai_stub, monkeypatch):
    models = _FakeModels()
    p = _provider(monkeypatch, models)
    p.batch_size = 2

    vecs = p.embed(["a", "b", "c", "d", "e"])

    assert len(vecs) == 5
    assert [len(c["contents"]) for c in models.calls] == [2, 2, 1]


def test_empty_input_makes_no_api_call(genai_stub, monkeypatch):
    models = _FakeModels()
    p = _provider(monkeypatch, models)
    assert p.embed([]) == []
    assert models.calls == []


def test_missing_api_key_names_the_config_key(genai_stub, monkeypatch):
    monkeypatch.setattr("app.plugin.config_store.plugin_config", lambda name: {})
    p = GeminiEmbeddingProvider()
    with pytest.raises(RuntimeError, match="embedding.gemini_api_key"):
        p.embed(["a"])


def test_registered_in_provider_registry():
    from app.plugin.embedding_provider import _PROVIDERS

    assert _PROVIDERS["gemini-embedding-2"] is GeminiEmbeddingProvider
    assert GeminiEmbeddingProvider.dim == 1536


def test_default_provider_is_still_local(monkeypatch):
    """The migration must not flip the default as a side effect of landing."""
    assert get_provider().dim == 384


def test_token_bucket_burst_is_not_the_minute_budget():
    """Sized at the minute's budget the bucket starts full and releases
    everything at once — the exact behaviour it exists to prevent."""
    p = GeminiEmbeddingProvider()
    assert p._limiter.capacity < p.docs_per_min
    assert p._limiter.capacity == p.batch_size * 2


def test_penalize_holds_the_whole_pool():
    """The quota is per-project, so one worker's 429 applies to all of them."""
    p = GeminiEmbeddingProvider()
    p._limiter.tokens = p._limiter.capacity
    p._limiter.penalize(30)
    assert p._limiter.tokens == 0.0
    assert p._limiter.blocked_until > 0


def test_embed_call_records_the_embed_corpus_role(genai_stub, monkeypatch):
    """W2 chunk 2: `_record_embed_usage()` (called from `_embed_batch()` on
    every success/failure) tags its `ai_usage` row with role='embed.corpus'
    — this is the multi-binding role's one write path."""
    from app.services import ai_ledger

    captured = {}
    monkeypatch.setattr(ai_ledger, "record", lambda **kw: captured.update(kw))

    models = _FakeModels()
    p = _provider(monkeypatch, models)
    p.embed(["a"])

    assert captured["role"] == "embed.corpus"
    assert captured["model"] == "gemini-embedding-2"


def test_embed_row_is_priced_from_a_real_token_count(genai_stub, monkeypatch):
    """2026-09-02: 1,350 embedding rows sat at NULL cost because the response
    carries no token count and the model had no rate. The model answers
    count_tokens, and the rate row now exists, so a batch is priced."""
    from app.services import ai_ledger

    captured = {}
    monkeypatch.setattr(ai_ledger, "record", lambda **kw: captured.update(kw))

    models = _FakeModels()
    models.count_tokens = lambda model, contents: type("T", (), {"total_tokens": 1_000_000})()
    p = _provider(monkeypatch, models)
    p.embed(["a", "b"])

    assert captured["units_in"] == 1_000_000
    assert captured["input_rate"] == 0.20 and captured["output_rate"] == 0.0
    assert captured["cost_usd"] == pytest.approx(0.20)


def test_embed_row_without_a_count_is_unknown_not_free(genai_stub, monkeypatch):
    from app.services import ai_ledger

    captured = {}
    monkeypatch.setattr(ai_ledger, "record", lambda **kw: captured.update(kw))

    models = _FakeModels()
    models.count_tokens = lambda model, contents: (_ for _ in ()).throw(RuntimeError("no"))
    p = _provider(monkeypatch, models)
    p.embed(["a"])

    assert captured["units_in"] == 0
    assert captured["cost_usd"] is None
