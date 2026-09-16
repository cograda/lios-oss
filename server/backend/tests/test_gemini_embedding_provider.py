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

from app.plugin.embedding_provider import (
    GeminiEmbeddingProvider,
    GeminiRateLimitError,
    get_provider,
)


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


# ---------------------------------------------------------------------------
# 2026-09-07: wait-on-429 instead of falling back, and token-based batching
# ---------------------------------------------------------------------------

class _RateLimitedError(Exception):
    """Stands in for whatever the SDK raises for a 429/RESOURCE_EXHAUSTED."""

    def __init__(self, message="429 RESOURCE_EXHAUSTED", code=429, retry_after=None):
        super().__init__(message)
        self.code = code
        if retry_after is not None:
            self.retry_after = retry_after


class _FlakyModels:
    """Raises for the first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times, dim=1536, exc_factory=None):
        self.fail_times = fail_times
        self.dim = dim
        self.calls = 0
        self.exc_factory = exc_factory or (lambda: _RateLimitedError())

    def embed_content(self, *, model, contents, config):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return _FakeResponse([_FakeEmbedding([0.1] * self.dim) for _ in range(len(contents))])


@pytest.fixture
def fake_clock(monkeypatch):
    """A monotonic clock that only advances when something calls `time.sleep`
    — so a 15s/30s/60s backoff schedule is exercised deterministically
    without the test suite actually waiting on it."""

    class _Clock:
        def __init__(self):
            self.t = 1_000.0

        def monotonic(self):
            return self.t

        def sleep(self, seconds):
            self.t += seconds

    clock = _Clock()
    monkeypatch.setattr("app.plugin.embedding_provider.time.monotonic", clock.monotonic)
    monkeypatch.setattr("app.plugin.embedding_provider.time.sleep", clock.sleep)
    return clock


def test_429_retries_in_place_and_succeeds(genai_stub, monkeypatch, fake_clock):
    """The default path: a couple of 429s, then a success — never raises,
    never touches the CPU fallback (there's nothing here to fall back to;
    that's `test_rate_limited_primary_does_not_fall_back` in the pipeline
    suite)."""
    models = _FlakyModels(fail_times=2)
    p = _provider(monkeypatch, models)

    vecs = p.embed(["a"])

    assert len(vecs) == 1
    assert models.calls == 3


def test_429_honors_retry_after_over_the_fixed_backoff(genai_stub, monkeypatch, fake_clock):
    models = _FlakyModels(fail_times=1, exc_factory=lambda: _RateLimitedError(retry_after=5))
    p = _provider(monkeypatch, models)

    p.embed(["a"])

    # Waited the hinted 5s, not the fixed table's 15s.
    assert fake_clock.t == 1_005.0


def test_429_exhausting_its_wait_budget_raises_rate_limit_error_not_runtime(
    genai_stub, monkeypatch, fake_clock,
):
    """Persisting past `rate_limit_max_wait_seconds` gives up FOR THIS TICK —
    but as a `GeminiRateLimitError`, distinct from the generic `RuntimeError`
    a genuine failure raises, so `_embed_and_store` knows not to fall back."""
    models = _FlakyModels(fail_times=999)
    p = _provider(monkeypatch, models)
    p.rate_limit_max_wait_seconds = 10.0

    with pytest.raises(GeminiRateLimitError, match="still rate-limited"):
        p.embed(["a"])

    # Gave up quickly (bounded by the wait budget), not after 8 long retries.
    assert fake_clock.t - 1_000.0 <= 10.0
    assert models.calls <= 3


def test_gemini_rate_limit_error_is_a_runtime_error(genai_stub, monkeypatch, fake_clock):
    """Subclassing matters: existing `except RuntimeError` callers upstream
    still catch it, only `_embed_and_store`'s `isinstance` check treats it
    differently."""
    models = _FlakyModels(fail_times=999)
    p = _provider(monkeypatch, models)
    p.rate_limit_max_wait_seconds = 1.0

    with pytest.raises(RuntimeError):
        p.embed(["a"])


def test_5xx_is_not_treated_as_a_rate_limit(genai_stub, monkeypatch, fake_clock):
    """A genuine provider failure keeps the old, much shorter exponential
    backoff and eventually raises a plain RuntimeError (never
    GeminiRateLimitError) — the caller falls back to another space for this,
    unlike a 429."""
    models = _FlakyModels(
        fail_times=999,
        exc_factory=lambda: _RateLimitedError("internal error", code=503),
    )
    p = _provider(monkeypatch, models)
    p.max_retries = 3

    with pytest.raises(RuntimeError, match="failed after 3 attempts") as exc_info:
        p.embed(["a"])

    assert not isinstance(exc_info.value, GeminiRateLimitError)
    assert models.calls == 3


def test_auth_error_is_not_treated_as_a_rate_limit(genai_stub, monkeypatch, fake_clock):
    """A 4xx other than 429 (e.g. a revoked/invalid key) is a statement about
    the request, not the server's mood — it must raise immediately, not
    retry for two minutes and not be mistaken for a rate limit."""
    models = _FlakyModels(
        fail_times=999,
        exc_factory=lambda: _RateLimitedError("invalid API key", code=401),
    )
    p = _provider(monkeypatch, models)

    with pytest.raises(RuntimeError, match="not retryable"):
        p.embed(["a"])

    assert models.calls == 1


def test_batches_are_split_by_token_budget(genai_stub, monkeypatch):
    """The item-count cap (`batch_size`) alone isn't enough — a batch of
    unusually long documents can burst well past a per-minute quota before
    hitting it. The chars/4 estimate is deliberately crude; it only needs to
    be in the right ballpark."""
    models = _FakeModels()
    p = _provider(monkeypatch, models)
    p.batch_size = 10  # generous, so only the token cap bites
    p.max_batch_tokens = 100

    texts = ["x" * 200] * 5  # ~50 tokens each

    p.embed(texts)

    assert [len(c["contents"]) for c in models.calls] == [2, 2, 1]


def test_oversized_single_item_goes_alone(genai_stub, monkeypatch):
    """An item over the token budget by itself is still sent — never
    dropped, never merged with anything else."""
    models = _FakeModels()
    p = _provider(monkeypatch, models)
    p.max_batch_tokens = 10

    texts = ["x" * 200, "y" * 4]  # ~50 tokens (over budget alone), ~1 token

    p.embed(texts)

    assert [len(c["contents"]) for c in models.calls] == [1, 1]


def test_max_batch_tokens_is_configurable(genai_stub, monkeypatch):
    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: pytypes.SimpleNamespace(gemini_api_key="test-key", max_batch_tokens=40),
    )
    models = _FakeModels()
    p = GeminiEmbeddingProvider()
    p._client = _FakeClient(models)
    p.batch_size = 10

    texts = ["x" * 200] * 3  # ~50 tokens each -> only 1 fits under a 40-token cap

    p.embed(texts)

    assert [len(c["contents"]) for c in models.calls] == [1, 1, 1]
