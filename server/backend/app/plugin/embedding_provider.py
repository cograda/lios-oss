"""Embedding provider interface — V4 chunk 3.4.

Swappable embedding backend. `EmbeddingProvider` is the interface any
backend must satisfy (a model identifier, its vector dimension, and a
batch-embed function); `FastEmbedProvider` — BAAI/bge-small-en-v1.5 via
fastembed, 384-dim — is the only implementation today. Selected by the
kernel setting `HomeSettings.embedding_provider` (`app/config.py`); the only
valid value right now is `"fastembed-bge-small"`.

This module intentionally does NOT replace the operational embedding code in
`app/services/embedding.py` (`get_model()` / `_embed_via_subprocess()`) —
those stay in place exactly as they were so existing tests that monkeypatch
them keep working, and so the lazy-in-process-model-for-search vs.
subprocess-isolated-batch-worker split (a memory-management concern, not a
provider-swapping concern) doesn't get tangled up with this interface.
`FastEmbedProvider` is a self-contained, independently correct
implementation of the `EmbeddingProvider` contract — the thing a future
second provider would need to match — and is what `app/services/embedding.py`
sources `MODEL_NAME`/`VECTOR_DIM` from, so the two can't silently drift.

Swapping the model is: implement a new `EmbeddingProvider`, register it in
`_PROVIDERS`, change `HOME_EMBEDDING_PROVIDER`, and re-embed (the
`embeddings.model_name` column, added alongside this module, makes stale
rows — rows whose `model_name` doesn't match the active provider — visible
via `search_stats`'s by-model breakdown; the re-embed pipeline itself is out
of scope for this chunk).

W2 chunk 2 (`app/services/ai_roles.py`) generalises this module's shape —
"config maps to a swappable binding, resolved through one accessor" — into
the AI role registry, for the `embed.corpus` role. This module is untouched
by that: `ai_roles.resolve_embed_corpus()` is a thin wrapper over
`get_providers()` below, so `_active_spaces()` and everything else here
keeps its exact pre-existing semantics.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Subprocess timeout for a single isolated batch embed. A 100-item batch on
# CPU takes roughly 5-15s incl. ~2s fastembed init; 5 min is a generous
# ceiling. Mirrors the constant previously inlined in
# app/services/embedding.py.
EMBED_SUBPROCESS_TIMEOUT_SECONDS = 300


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Contract every embedding backend must satisfy."""

    provider_id: str
    model_name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one vector per text."""
        ...

    def available(self) -> bool:
        """Whether this provider can be used right now.

        Distinct from "configured correctly": this is a cheap, local check
        (is the API key present?) used to decide whether to *attempt* a space
        at all. A provider that returns False is skipped silently — a
        deployment that has never set a Gemini key should run happily on the
        local space alone, not log a failure every five minutes.
        """
        ...


class FastEmbedProvider:
    """BAAI/bge-small-en-v1.5 via fastembed (384-dim).

    `embed()` lazy-loads the model once per process and keeps it resident —
    fine for occasional, low-volume calls (this is what a future non-search
    caller of the generic provider interface would use). The high-volume
    batch worker path (`EmbeddingService.process_queue`) does NOT call this;
    it uses `app.services.embedding._embed_via_subprocess()` instead, which
    shells out to a short-lived subprocess so the ONNX/fastembed memory
    arenas are reclaimed every cycle rather than retained in the long-running
    server process. Both paths produce identical vectors for the same model.
    """

    provider_id = "fastembed-bge-small"
    model_name = "BAAI/bge-small-en-v1.5"
    dim = 384

    # Input beyond this is read by nobody: bge-small is a 512-token BERT and
    # truncates there. Measured 2026-08-06 against a 32,000-char input —
    # cosine vs the full text is 0.9891 at 1,500 chars, 0.9998 at 2,500 and
    # **exactly 1.000000 from 4,000 onward**. 6,000 is that ceiling with room
    # for unusually long tokens, and no natural text reaches it.
    #
    # This is not a quality knob, it is a cost one, and it has bitten: raising
    # MAX_CHUNK_CHARS to 30,000 for gemini's benefit sent 5x the text through
    # this model for identical vectors, and a 500-item batch of it exhausted
    # the server's 7.8 GB — three concurrent embed subprocesses, 197 MB free,
    # with Postgres on the same box. **A shared cap has to be the maximum any
    # provider can use; each provider then trims to what it can actually read.**
    max_chars = 6_000

    _model = None  # class-level cache, mirrors the old module-level `_model`

    def available(self) -> bool:
        """Always. The model ships in the image and needs no network."""
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if FastEmbedProvider._model is None:
            from fastembed import TextEmbedding

            FastEmbedProvider._model = TextEmbedding(self.model_name)
        clipped = [t[: self.max_chars] for t in texts]
        return [vec.tolist() for vec in FastEmbedProvider._model.embed(clipped)]

    def embed_isolated(self, texts: list[str]) -> list[list[float]]:
        """Embed via a short-lived subprocess (process-isolated).

        Equivalent implementation of the logic in
        `app.services.embedding._embed_via_subprocess` — kept here too so
        `FastEmbedProvider` is a complete, independently-usable
        implementation of `EmbeddingProvider`, not just a shell around the
        service module.
        """
        if not texts:
            return []

        payload = json.dumps(texts)
        proc = subprocess.run(
            [sys.executable, "-m", "app.services.embed_subprocess"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=EMBED_SUBPROCESS_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"embed_subprocess failed (rc={proc.returncode}): {proc.stderr[:500]}"
            )
        vectors = json.loads(proc.stdout)
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"embed_subprocess returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return vectors


class _TokenBucket:
    """Paced admission for a per-project quota.

    Ported from timemachine's limiter rather than reinvented, including the two
    details it records as having cost a restart:

    - **Burst capacity is not the minute's budget.** Sized at the full budget the
      bucket starts full and releases every document at once, which is the exact
      behaviour it exists to prevent. A couple of batches only.
    - **A 429 must hold every worker, not just the one that saw it.** The quota is
      per-project, so one thread's rejection is information about all of them.

    Retry-on-429 alone is not a rate limiter: with N workers all pushing, the
    steady state is "everyone is always slightly over", and the run spends its
    time being rejected. Pacing at the known ceiling keeps 429 an exception.
    """

    def __init__(self, per_minute: int, burst: int):
        self.rate = per_minute / 60.0
        self.capacity = float(max(burst, 1))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.blocked_until = 0.0
        self.lock = threading.Lock()

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)
            self.tokens = 0.0

    def acquire(self, n: int) -> None:
        n = min(n, self.capacity)  # never ask for more than the bucket can hold
        while True:
            with self.lock:
                now = time.monotonic()
                wait = self.blocked_until - now
                if wait <= 0:
                    self.tokens = min(
                        self.capacity, self.tokens + (now - self.updated) * self.rate
                    )
                    self.updated = now
                    if self.tokens >= n:
                        self.tokens -= n
                        return
                    wait = (n - self.tokens) / self.rate
            time.sleep(max(wait, 0.01))


class GeminiEmbeddingProvider:
    """gemini-embedding-2, 1536-dim (Matryoshka-truncated from 3072).

    Chosen over bge-small on measurement, not reputation: 10-NN overlap between
    the two spaces is only ~0.23, i.e. the ordering retrieval depends on is
    substantially different, and an A/B over 19 queries improved 15 of them
    (sign test p = 0.019). See
    `vault/Projects/lios/Plans/embedding-migration-gemini-1536.md`.

    Three properties of this model that are easy to get wrong, all verified
    against the live API in timemachine (2026-08-03) rather than assumed:

    1. **A plain `list[str]` does not batch.** The SDK folds the list into the
       parts of a *single* document and the API returns ONE embedding for the
       whole batch — with no error at all. Each text must be its own `Content`.
       This is guarded twice: by construction below, and by a count assertion,
       because a short result list paired positionally with ids would attach
       every id to the wrong vector, silently.
    2. **No `task_type` parameter.** `gemini-embedding-001` had one; this model
       replaced it with natural-language task instructions in the prompt. So the
       query path prepends an instruction and the document path must not — the
       corpus was embedded document-side, and a query embedded document-side
       lands in a different part of the space.
    3. **It auto-normalises at `output_dimensionality < 3072`** (001 did not) —
       verified L2 = 1.0 exactly. So no renormalisation here, but the width is
       asserted: a 3072-wide vector in a 1536 column is a hard failure, and a
       silently truncated one is worse.

    No subprocess-isolated path is needed (contrast `FastEmbedProvider`): there
    is no local ONNX arena to reclaim, just an HTTP call.
    """

    provider_id = "gemini-embedding-2"
    model_name = "gemini-embedding-2"
    dim = 1536
    batch_size = 64
    # Documents/min for the paid tier is 3,000, and the metric counts embedded
    # *contents*, not HTTP requests — measured at ~270 req/min but ~18,000
    # docs/min before dying. So a bigger batch buys throughput only up to this
    # ceiling and nothing beyond it. Held under the cap because the window is
    # server-side and our clock isn't theirs.
    docs_per_min = 2400
    # 429s are expected steady state and each may ask for a full ~50s quota
    # window, so the retry budget has to outlast one window.
    max_retries = 8
    max_chars = 30_000  # ~8k tokens of context; longer inputs are truncated

    query_instruction = (
        "Task: retrieve personal messages, emails, notes and documents "
        "relevant to this query.\nQuery: "
    )

    def __init__(self) -> None:
        self._client = None
        self._limiter = _TokenBucket(self.docs_per_min, burst=self.batch_size * 2)

    def available(self) -> bool:
        """True when an API key is configured. No network call — see protocol.

        `plugin_config()` returns a **typed pydantic model** built from the
        manifest's `config_schema`, not a dict — so this is attribute access,
        not `.get()`. The dict version raised `AttributeError` and the broad
        `except Exception` below turned that into a quiet "not configured",
        which is how it survived being written: the failure mode of the bug and
        the correct answer for an unconfigured deployment were identical.
        `OperationalError` is caught deliberately (no DB during boot or in a unit
        test); type errors are not.
        """
        from sqlalchemy.exc import SQLAlchemyError

        from app.plugin.config_store import plugin_config

        try:
            cfg = plugin_config("embedding")
        except SQLAlchemyError:
            return False
        return bool(getattr(cfg, "gemini_api_key", None))

    def _get_client(self):
        if self._client is None:
            from google import genai

            from app.plugin.config_store import plugin_config

            # Typed pydantic model, not a dict — see available().
            api_key = getattr(plugin_config("embedding"), "gemini_api_key", None)
            if not api_key:
                raise RuntimeError(
                    "embedding.gemini_api_key is not configured — set it via "
                    "PUT /api/integrations/embedding/config before selecting "
                    "the gemini provider"
                )
            self._client = genai.Client(api_key=api_key)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed documents. Never applies `query_instruction` — see docstring."""
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._embed_batch(texts[i : i + self.batch_size]))
        return out

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query, instruction-prefixed to match the corpus side."""
        return self._embed_batch([self.query_instruction + text])[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        from google.genai import types

        client = self._get_client()
        # One Content per text. A plain list[str] silently returns a single
        # embedding for the whole batch — see docstring point 1.
        contents = [
            types.Content(parts=[types.Part(text=t[: self.max_chars])]) for t in texts
        ]

        started = time.time()
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._limiter.acquire(len(contents))
            try:
                resp = client.models.embed_content(
                    model=self.model_name,
                    contents=contents,
                    config=types.EmbedContentConfig(output_dimensionality=self.dim),
                )
                vecs = [list(e.values) for e in resp.embeddings]
                if len(vecs) != len(contents):
                    raise RuntimeError(
                        f"asked for {len(contents)} embeddings, got {len(vecs)} — "
                        "batching cannot be trusted; drop batch_size to 1"
                    )
                for v in vecs:
                    if len(v) != self.dim:
                        raise RuntimeError(
                            f"{self.model_name} returned {len(v)}-dim vectors, "
                            f"expected {self.dim} — refusing to write a "
                            "wrong-width vector into the index"
                        )
                _record_embed_usage(self.model_name, contents, started, ok=True, client=client)
                return vecs
            except RuntimeError:
                raise  # our own assertions — never retried, never masked
            except Exception as exc:
                last_exc = exc
                # A 4xx other than 429 is a statement about the request, not
                # about the server's mood: retrying it eight times with
                # exponential backoff spends two minutes to reach the same
                # answer, and buries the real cause under seven identical
                # warnings. 429 is the one client error that *is* temporal.
                code = getattr(exc, "code", None)
                if isinstance(code, int) and 400 <= code < 500 and code != 429:
                    raise RuntimeError(
                        f"gemini embed rejected the request ({code}, not retryable): "
                        f"{str(exc)[:300]}"
                    ) from exc
                if attempt == self.max_retries - 1:
                    break
                sleep = 2**attempt
                # Hold the whole pool: the quota is per-project, so this
                # rejection applies to every worker, not just this one.
                self._limiter.penalize(sleep)
                logger.warning(
                    "gemini embed retry %d/%d in %.1fs: %s",
                    attempt + 1, self.max_retries, sleep,
                    # 300 chars, not 90: truncation hid the decisive part of an
                    # error (quotaId, billing-vs-quota wording) repeatedly.
                    str(exc)[:300],
                )
        _record_embed_usage(
            self.model_name, contents, started, ok=False,
            error=str(last_exc)[:300] if last_exc else "exhausted retries",
            client=client,
        )
        raise RuntimeError(
            f"gemini embed failed after {self.max_retries} attempts: "
            f"{str(last_exc)[:300]}"
        )


def _count_tokens(client, model: str, contents) -> int | None:
    """One `count_tokens` call per batch, so an embedding row carries a real
    token count rather than a text count. Best-effort: None means unknown."""
    if client is None:
        return None
    try:
        resp = client.models.count_tokens(model=model, contents=contents)
        total = getattr(resp, "total_tokens", None)
        return int(total) if total is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("embedding_provider: count_tokens failed: %s", exc)
        return None


def _record_embed_usage(
    model: str,
    contents,
    started: float,
    *,
    ok: bool,
    error: str | None = None,
    client=None,
) -> None:
    """Best-effort ai_usage row for one `embed_content` batch call.

    `embed_content`'s response carries no token count, so until 2026-09-02
    `units_in` here was the number of texts sent and `cost_usd` was always
    NULL — 1,350 rows of "unknown", the largest call volume in the ledger.
    The embedding model *does* answer `count_tokens`, so each batch now
    spends one free call to learn its token count, and the cost comes from
    `coglib.llm.MODELS` like every other row. If counting fails the row
    records 0 tokens and NULL cost — unknown, still never free. Never raises.
    """
    from app.services import ai_ledger

    try:
        tokens = _count_tokens(client, model, contents)
        input_rate = output_rate = cost = None
        try:
            from coglib import llm as _llm
            spec = _llm.MODELS.get(model)
        except Exception:  # noqa: BLE001
            spec = None
        if spec is not None:
            input_rate, output_rate = spec.input_rate, spec.output_rate
            if tokens is not None:
                cost = tokens * input_rate / 1e6
        ai_ledger.record(
            provider="google",
            model=model,
            kind="embedding",
            caller="integration:embedding",
            role="embed.corpus",
            units_in=tokens or 0,
            latency_ms=int((time.time() - started) * 1000),
            cost_usd=cost,
            input_rate=input_rate,
            output_rate=output_rate,
            ok=ok,
            error=error,
        )
    except Exception:  # noqa: BLE001
        logger.debug("embedding_provider: failed to record ai_usage row", exc_info=True)


# Registry of provider-id -> class. Provider ids are stable strings (not
# model names) so a future provider can change its underlying model without
# renaming the config value everyone already has set.
_PROVIDERS: dict[str, type] = {
    "fastembed-bge-small": FastEmbedProvider,
    "gemini-embedding-2": GeminiEmbeddingProvider,
}


def _configured_ids() -> list[str]:
    """Ordered provider ids from `HomeSettings.embedding_provider`.

    Accepts a comma-separated list — first entry is the search default, the
    rest are fallbacks in order. A bare single value (every deployment before
    Phase 2) parses to a one-element list, so nothing needs reconfiguring.
    """
    from app.config import settings

    ids = [part.strip() for part in (settings.embedding_provider or "").split(",")]
    ids = [i for i in ids if i]
    if not ids:
        raise ValueError("embedding_provider is empty — expected at least one provider id")

    seen: set[str] = set()
    ordered: list[str] = []
    for i in ids:
        if i not in _PROVIDERS:
            raise ValueError(
                f"Unknown embedding_provider {i!r} — valid options: {sorted(_PROVIDERS)}"
            )
        if i not in seen:  # a duplicate would double-write the same space
            seen.add(i)
            ordered.append(i)
    return ordered


def get_providers() -> list[EmbeddingProvider]:
    """Every configured provider, search default first.

    Order is meaningful in two different ways, and they are not the same thing:

    - **Search** walks this list and answers from the first space that responds,
      so the order is a preference ranking.
    - **Writing** targets every available space, so the order is irrelevant
      there — both get written, and coverage gaps are closed by a backfill.

    What must never happen is a query embedded in one space being scored
    against vectors from another. Each space is self-contained end to end.
    """
    return [_PROVIDERS[i]() for i in _configured_ids()]


def get_provider() -> EmbeddingProvider:
    """The search-default provider (first configured).

    Kept as the single-provider accessor every pre-Phase-2 caller already uses,
    so `MODEL_NAME`/`VECTOR_DIM` and anything else asking "the" model still get
    a sensible answer.
    """
    return get_providers()[0]
