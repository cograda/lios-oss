"""Unified embedding pipeline — queue, worker, and search.

Replaces per-integration embedding logic with a single pipeline:
1. Integrations enqueue content via EmbeddingService.enqueue()
2. Background worker (every 5 min) processes the queue in batches
3. Unified 'embeddings' table enables cross-source semantic search

Models: chosen by `embedding_provider` (app/plugin/embedding_provider.py, V4
chunk 3.4), which since Phase 2 is an ORDERED, COMMA-SEPARATED LIST — the first
entry is the search default and the rest are fallbacks.

Two consequences run through this module:

1. **The live worker writes the primary space only; other spaces are caught up
   nightly (2026-09-07).** Measured on production: a 100-item batch takes
   80-130s, of which the Gemini (primary) calls are ~2s and the local
   `bge-small` fastembed subprocess is ~70-90s — i.e. embedding into every
   active space on the hot path was spending nearly all of a batch's wall
   time on a space nobody's queries answer from (`get_providers()`'s order is
   a search-preference ranking; only the first entry is ever queried unless
   it's down). `process_queue`'s default `spaces="primary"` embeds a queued
   item into `get_providers()[0]`'s space only and marks it 'done'; the
   `embedding_space_backfill` nightly cron (`app/integrations/embedding/
   tasks.py`) walks the anti-join for every other active space and fills the
   gap, reusing `backfill.fill_space` — the same walker `fill-space` (the CLI)
   already used to turn a new provider on. `spaces="all"` is still available
   (the previous default) for callers that want every space written inline,
   e.g. `reembed.py`'s post-reclean drain, where the text itself changed and
   every space needs to see it, not just the primary.
2. **If the primary space fails for a batch, the item falls back to the next
   available space rather than being deferred.** A batch still becomes
   searchable today even during a primary-provider outage; the nightly
   backfill closes the primary's gap once it recovers. Only when *every*
   active space fails does the old bisect/retry/give-up path (below) engage.
3. **A space can lag without blocking the others.** If a space fails for a
   batch, the others still store, the item is still 'done', and the gap shows
   up as `missing` in `stats()["spaces"]` — an anti-join a backfill can close.
   Requiring all spaces would mean a remote provider's outage stops the *local*
   one embedding, which is precisely backwards.
4. **A query is always scored in one space.** A query is embedded into a
   single space and scored only within it — cosine distance between
   different models' vectors is meaningless — nothing here joins across
   spaces, and `search()` reports which one answered. Independent of the
   write-time policy above: a space still has full read-time fallback the
   moment it has coverage, whether that coverage arrived same-cycle (primary)
   or overnight (backfill).

V4 chunk 3.4 moved the `Embedding`/`EmbeddingQueue` ORM classes into
`app.integrations.embedding.models` (that package now owns them, like any
other integration owns its models) — re-exported below so every existing
importer of `app.services.embedding.{Embedding,EmbeddingQueue}` keeps working
unchanged. This module stays the shared API surface (enqueue/search/stats/
delete) every producer and consumer imports from.
"""

import hashlib
import json
import logging
import subprocess
from typing import Any, Callable  # noqa: F401 — used in string annotations
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import func, or_, text, tuple_
from sqlalchemy.orm import Session

from app.auth.context import current_user_id_or_none
from app.integrations.embedding.models import (  # noqa: F401 — compat re-export
    Embedding,
    EmbeddingQueue,
    EmbeddingVecBgeSmall384,
    EmbeddingVecGemini1536,
    VECTOR_MODELS,
)
from app.integrations.embedding.cleaning import CLEANER_VERSION, clean
from app.plugin.embedding_provider import (
    FastEmbedProvider,
    GeminiRateLimitError,
    _PROVIDERS,
    get_provider,
    get_providers,
)
from app.services import vault_scope

logger = logging.getLogger(__name__)

# Path prefixes `near_duplicates` skips unless a caller says otherwise.
# Template-generated notes are near-identical to each other by construction —
# `Daily Notes/` share frontmatter, a heading skeleton and two task queries, so
# a quiet Tuesday scores 0.985 against a quiet Wednesday. Measured on the real
# vault: 45 of the first 60 unscoped pairs were Daily-Notes-to-Daily-Notes,
# burying every genuine duplicate beneath them.
DEFAULT_DUPLICATE_EXCLUDES = ("Daily Notes/", "Weekly Reviews/")

# Safety net only, applied *after* cleaning, so no producer can send unbounded
# text to a metered embedder. Truncation is logged, never silent.
#
# Raised from 8,000 to 30,000 on 2026-08-06: a dry run over production found
# **523 historical_corpus chunks above 8,000 chars, the longest 138,809**, all
# losing everything past the cap. 30,000 is the ceiling of the most capable
# configured embedder (gemini-embedding-2, ~8k tokens); bge-small truncates
# itself at 512 tokens (~2,000 chars) so this costs it nothing either way.
#
# This is a mitigation, not the fix. A 138,809-char chunk is an upstream
# chunking failure: it should be ~17 chunks with their own ids and their own
# vectors, not one chunk whose tail is discarded. Fixing that changes chunk
# identity (`chunk_index` is currently always 0) and therefore the dedup key,
# so it is its own piece of work.
MAX_CHUNK_CHARS = 30_000


# ---------------------------------------------------------------------------
# R4: retrieval provenance and recency (2026-09-04)
#
# The bug this exists to fix, measured 2026-08-13: a `.stversions` snapshot of
# a vault note from 1 August outranked the live file at 0.7651 vs 0.7600 — a
# record of past belief beating current truth on cosine distance alone, by an
# amount too small to ever be "wrong" on its own terms. Excluding `.stversions`
# from indexing (obsidian/sync.py SKIP_DIRS) is a tourniquet: it only helps the
# one path a producer remembered to filter, and does nothing for a genuinely
# superseded note (frontmatter `status: done`/`superseded`) that is still
# indexed on purpose so it can be found and labelled, not hidden.
#
# The treatment is two independent, additive things:
#   1. **Provenance**, attached by the producer at write time into
#      `metadata_json` — `source_date` (the real-world date the content is
#      from) and `is_history` (a boolean the producer asserts: this chunk is a
#      superseded/archived record, not current truth). Read time then derives
#      `stale` (age past a configurable threshold) — a label, never a filter,
#      same reasoning `vault_search`'s `status` field already documents: a
#      search that silently drops things produces confidently wrong "nothing
#      found" answers.
#   2. **Recency decay**, applied only in `search()` (not `similar_to()` or
#      `near_duplicates()`, which compare content for its own sake — see each
#      call site below for why). `score *= 0.5 ** (age_days / half_life)`,
#      plus a further fixed multiplier for `is_history` chunks. A producer
#      that never sets `source_date`/`is_history` still gets safe defaults
#      (falls back to `created_at`, `is_history=False`) rather than an error —
#      provenance is additive, not a required contract change for every
#      existing writer.
_HISTORY_PENALTY = 0.85  # fixed multiplier for is_history chunks, on top of
# ordinary recency decay. Not a config key like the half-life below: there is
# no real per-deployment range to explore here (a chunk the producer has
# itself flagged as a superseded/archived record should always additionally
# lose to an otherwise-equal live one), so a knob would only invite tuning a
# problem that isn't actually variable.

_DEFAULT_RECENCY_HALF_LIFE_DAYS = 30.0
_DEFAULT_STALENESS_THRESHOLD_DAYS = 180.0


def _recency_settings() -> tuple[float, float]:
    """(half_life_days, staleness_threshold_days) from the embedding manifest's
    config, falling back to the hardcoded defaults above on any config
    problem — degrading to a safe default beats failing every search.
    """
    from app.plugin.config_store import plugin_config

    try:
        cfg = plugin_config("embedding")
        half_life = float(cfg.recency_half_life_days)
        threshold = float(cfg.staleness_threshold_days)
    except Exception:
        logger.warning("recency config unavailable; using hardcoded defaults", exc_info=True)
        return _DEFAULT_RECENCY_HALF_LIFE_DAYS, _DEFAULT_STALENESS_THRESHOLD_DAYS
    if half_life <= 0:
        half_life = _DEFAULT_RECENCY_HALF_LIFE_DAYS
    if threshold <= 0:
        threshold = _DEFAULT_STALENESS_THRESHOLD_DAYS
    return half_life, threshold


def _decay_factor(age_days: float, half_life_days: float) -> float:
    """Exponential recency decay: the score halves every `half_life_days`.

    Age is clamped to >= 0 — a future `source_date` (clock skew, a bad write)
    must never produce a decay > 1.0 that boosts a result above its raw
    cosine score.
    """
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def _chunk_provenance(metadata_json: str | None, created_at, staleness_threshold_days: float) -> dict:
    """Derive (source_date, is_history, stale, age_days) for one chunk.

    `source_date` and `is_history` are producer-supplied (in `metadata_json`);
    everything here is a read-time fallback/derivation, never a write. A
    producer that hasn't been updated for R4 yet gets `source_date=created_at`
    (the best available proxy — when the chunk was embedded) and
    `is_history=False` (the safe default: no unearned penalty).
    """
    meta = _parse_metadata(metadata_json) or {}

    source_date = None
    raw = meta.get("source_date")
    if raw:
        try:
            source_date = datetime.fromisoformat(raw)
            if source_date.tzinfo is None:
                source_date = source_date.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            source_date = None
    if source_date is None:
        source_date = created_at

    is_history = bool(meta.get("is_history", False))

    if source_date is not None:
        age_days = max((datetime.now(timezone.utc) - source_date).total_seconds() / 86400.0, 0.0)
    else:
        age_days = 0.0

    return {
        "source_date": source_date.isoformat() if source_date else None,
        "is_history": is_history,
        "stale": age_days > staleness_threshold_days,
        "age_days": age_days,
    }


def _parse_metadata(metadata_json: str | None) -> dict | None:
    """Best-effort parse of a chunk's metadata for cleaner routing.

    Never raises: metadata is an optional, producer-supplied blob, and a
    malformed one must degrade to "route by source alone", not fail the enqueue
    of otherwise-valid content.
    """
    if not metadata_json:
        return None
    try:
        parsed = json.loads(metadata_json)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _prepare_content(
    content: str,
    source: str,
    metadata_json: str | None = None,
    post_clean: "Callable[[str], str] | None" = None,
) -> str:
    """Clean, then cap. Order matters and is the whole point.

    Cleaning lives here — the one chokepoint every producer goes through — rather
    than in each integration, so no integration can forget it and every source
    gets the same treatment. `clean()` is source-aware, and for
    `historical_corpus` also chunk-type-aware, which is why the metadata comes
    along: that one source mixes email, chat, Claude transcripts, PDFs and
    spreadsheet rows, so the source string alone routes most of it wrongly.

    Capping *after* cleaning is the fix for a real defect: google_mail truncated
    the raw Gmail body at 4000 chars before anything stripped it, so for HTML mail
    the surviving text was frequently `<style>` blocks and table scaffolding while
    the actual prose fell off the end. Truncate-then-clean keeps the furniture and
    discards the signal.

    `post_clean` runs between cleaning and capping. It exists for
    `BoilerplateFilter`, which cannot live in `clean()`: it needs a corpus-wide
    `fit()` and this chokepoint sees one item at a time. Only the re-enqueue
    traversal, which holds the whole corpus, can supply it.

    **A caller that passes `post_clean` for a source whose producer re-offers
    content continuously will cause permanent re-embed churn** — the live path
    has no filter, so it would produce different text and a different hash on
    every cycle, forever. See `backfill.BOILERPLATE_SOURCES` for which sources
    are safe (the immutable archives) and why.
    """
    cleaned = clean(content or "", source, _parse_metadata(metadata_json))
    if post_clean is not None:
        cleaned = post_clean(cleaned)
    if len(cleaned) > MAX_CHUNK_CHARS:
        logger.warning(
            "embed content for source=%s exceeded %d chars after cleaning "
            "(%d) — truncating; consider chunking upstream",
            source, MAX_CHUNK_CHARS, len(cleaned),
        )
        cleaned = cleaned[:MAX_CHUNK_CHARS]
    return cleaned


def _content_hash(content: str) -> str:
    """Hash the cleaned text, salted with the cleaner version.

    The salt is what makes a cleaner change re-embed the corpus: dedup keys on
    this hash, so bumping CLEANER_VERSION invalidates every row deliberately
    rather than leaving old vectors built by an older cleaner silently in place.
    """
    return hashlib.md5(f"v{CLEANER_VERSION}:{content}".encode("utf-8")).hexdigest()


# Every registered provider must have somewhere to put its vectors. Checked
# at import time because the alternative is discovering it mid-batch, after a
# provider has already been added to config and the worker has run.
_missing_tables = set(_PROVIDERS) - set(VECTOR_MODELS)
_orphan_tables = set(VECTOR_MODELS) - set(_PROVIDERS)
if _missing_tables or _orphan_tables:
    raise RuntimeError(
        "embedding provider/vector-table registries disagree — "
        f"providers with no table: {sorted(_missing_tables)}; "
        f"tables with no provider: {sorted(_orphan_tables)}. "
        "Every entry in app.plugin.embedding_provider._PROVIDERS needs a "
        "matching entry in app.integrations.embedding.models.VECTOR_MODELS."
    )

# Sourced from the search-default provider so the two can't silently drift.
# With more than one space configured these describe the *default* space only;
# per-space detail is in `EmbeddingService.stats()["spaces"]`.
_provider = get_provider()
MODEL_NAME = _provider.model_name
VECTOR_DIM = _provider.dim


def _enable_iterative_scan(session: Session) -> None:
    """Make a *filtered* vector search return as many rows as it was asked for.

    Without this, `LIMIT` does not control the size of a filtered HNSW result —
    `hnsw.ef_search` does. The index walk produces `ef_search` candidates (40 by
    default) and the `WHERE` clause is applied *afterwards*, so a query scoped to
    one source keeps only the fraction of those candidates that happen to match.

    Measured on the live corpus 2026-08-15: `source='vault'` is 5,412 of 63,130
    embeddings (8.6%), and a `LIMIT 50` vault search returned **10 rows spanning
    1 document**. With iterative scan: **50 rows spanning 20 documents**. Every
    filtered semantic search in comar — vault, mail, folder- and status-scoped —
    had been silently capped this way since the HNSW index was created, and the
    symptom reads as "search is a bit narrow" rather than as a bug.

    `strict_order` rather than `relaxed_order`: relaxed is faster but may return
    rows slightly out of distance order, and these scores are shown to callers
    and compared against thresholds. Correct ordering is worth more here than
    the latency.

    Best-effort: pgvector < 0.8 has no such setting, and a non-Postgres backend
    has none either. Failing to widen the scan degrades results; raising here
    would take search down entirely, so it is deliberately swallowed.
    """
    try:
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    except Exception:  # noqa: BLE001 — see docstring
        session.rollback()
        logger.debug("iterative scan unavailable; filtered searches stay ef_search-bound")


def _active_spaces() -> list[tuple[object, type]]:
    """[(provider, vector-table model)] for every configured, available space.

    Availability is checked per call rather than cached: a Gemini key can be
    set through the dashboard while the server is running, and the next worker
    cycle should pick it up without a restart.
    """
    return [
        (p, VECTOR_MODELS[p.provider_id])
        for p in get_providers()
        if p.available()
    ]


def _embed_with(provider, texts: list[str]) -> list[list[float]]:
    """Embed with one provider, using the right execution path for it.

    fastembed goes through a short-lived subprocess so the ONNX arenas are
    reclaimed each cycle rather than retained in the long-running server
    process; a remote provider is just an HTTP call with nothing to reclaim.
    Dispatching on the class (not on a capability check) keeps
    `_embed_via_subprocess` the single monkeypatchable seam the test suite
    already targets.
    """
    if isinstance(provider, FastEmbedProvider):
        return _embed_via_subprocess(texts)
    return provider.embed(texts)


def _embed_query_with(provider, query: str) -> list[float]:
    """Embed a search query into one provider's space.

    Two asymmetries matter here. fastembed keeps using the in-process
    `get_model()` for search (the batch worker's subprocess isolation is a
    memory concern, not a correctness one, and this is the seam the tests
    patch). And a provider may need the query prepared differently from a
    document — gemini-embedding-2 has no `task_type` parameter and instead
    expects a natural-language task instruction, so a query embedded
    document-side lands in the wrong part of the space.
    """
    if isinstance(provider, FastEmbedProvider):
        return list(get_model().embed([query]))[0].tolist()
    embed_query = getattr(provider, "embed_query", None)
    if embed_query is not None:
        return embed_query(query)
    return provider.embed([query])[0]

# Subprocess timeout for a single batch embed. A 100-item batch on CPU
# takes roughly 5–15s incl. ~2s fastembed init; 5 min is a generous ceiling.
EMBED_SUBPROCESS_TIMEOUT_SECONDS = 300

# Give up on a queue item after this many failed embed attempts.
MAX_EMBED_ATTEMPTS = 3

# Arbitrary but fixed key for the advisory lock that makes queue processing
# single-flight across processes (scheduler cron vs a manual drain). Must never
# change, or an old and a new process would take different locks and both run.
_QUEUE_LOCK_KEY = 8_140_251_001


class UnknownEmbeddingSourceError(ValueError):
    """Raised when enqueue/search/delete_source gets an unregistered source.

    Every valid source string is declared by exactly one integration's
    manifest (`embedding_sources`, V4 chunk 1.1) — see
    `app.plugin.validate._check_unique_embedding_sources` for the
    startup-time duplicate-claim check. This is the runtime counterpart:
    a source nobody claims is almost certainly a typo, not a legitimate new
    source that just hasn't been registered.
    """


def _known_embedding_sources() -> set[str]:
    """{source string} claimed by any integration's manifest.

    Sourced live from `app.plugin.validate.discover_manifests()` — same
    registry chunk 1.2's freshness/model-discovery machinery uses. Not
    cached: manifests are static per-process, and every module involved is
    already import-cached, so the repeat cost is negligible (same pattern as
    `app/routes/integrations.py` and friends, which call this on every
    request).
    """
    from app.plugin.validate import discover_manifests

    sources: set[str] = set()
    for manifest in discover_manifests().values():
        sources.update(manifest.embedding_sources)
    return sources


def _check_known_source(source: str) -> None:
    known = _known_embedding_sources()
    if source not in known:
        raise UnknownEmbeddingSourceError(
            f"Unknown embedding source {source!r} — no integration manifest "
            f"declares it via embedding_sources. Known sources: {sorted(known)}"
        )

# Lazy-loaded model — only used by the live search() path. The batch worker
# now shells out to embed_subprocess.py so the main process doesn't retain
# the ONNX arenas between cycles.
_model = None


def get_model():
    """Get the shared fastembed model instance (lazy-loaded).

    Used by live search queries only. The batch worker uses
    _embed_via_subprocess() so model memory is reclaimed each cycle.
    """
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL_NAME)
    return _model


def _embed_via_subprocess(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts in a short-lived subprocess.

    The subprocess loads fastembed, embeds, writes vectors to stdout,
    and exits — the OS reclaims the ONNX/fastembed memory arenas
    rather than holding them in the long-running server process.
    """
    if not texts:
        return []

    # Trim to what bge-small can actually read (512 tokens) before the text
    # crosses a process boundary. Measured: cosine against the full text is
    # exactly 1.000000 from 4,000 chars, so this changes no vector — it only
    # stops us serialising, piping and tokenising up to 30,000 chars per item
    # for nothing. Doing it here rather than only in FastEmbedProvider.embed
    # because this subprocess path is the one the batch worker actually uses,
    # and a 500-item batch of untrimmed text exhausted a 7.8 GB server.
    limit = FastEmbedProvider.max_chars
    payload = json.dumps([t[:limit] for t in texts])
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


# Models (`Embedding`, `EmbeddingQueue`) now live in
# `app.integrations.embedding.models` — imported + re-exported above.

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class EmbeddingService:
    """Unified embedding pipeline: enqueue, process, search."""

    @staticmethod
    def enqueue(
        session: Session,
        source: str,
        source_id: str,
        content: str,
        metadata_json: str | None = None,
        user_id: int | None = None,
        post_clean: "Callable[[str], str] | None" = None,
    ) -> bool:
        """Add content to the embedding queue.

        Deduplicates by content_hash — if the same content is already
        queued or embedded, skips it. If content changed (same source_id,
        different hash), queues for re-embedding.

        user_id=None means household-shared (visible to all users in
        search); per-user sources (email, whatsapp) must pass the owner.

        Returns True if enqueued, False if skipped (unchanged).

        Raises UnknownEmbeddingSourceError if `source` isn't declared by any
        integration's manifest `embedding_sources`.
        """
        _check_known_source(source)
        content = _prepare_content(content, source, metadata_json, post_clean)

        # Cleaning can legitimately empty an item — a WhatsApp message that was
        # only a banner, a PDF page of only a page number, a conversation window
        # of only media placeholders. Such a chunk is not a retrievable unit: it
        # can never be a useful search hit, and it renders as a blank preview.
        #
        # This has to be enforced here rather than left to the providers,
        # because they disagree: fastembed embeds "" without complaint (42 such
        # rows accumulated silently in the local space), while Gemini rejects
        # the batch with `400 INVALID_ARGUMENT: contains an empty Part` — so
        # the junk was invisible until a second space refused it. Any existing
        # row is deleted rather than left behind: the source item still exists,
        # it simply has no embeddable content any more.
        if not content.strip():
            deleted = (
                session.query(Embedding)
                .filter_by(source=source, source_id=source_id, user_id=user_id)
                .delete(synchronize_session=False)
            )
            if deleted:
                logger.info(
                    "dropped %d embedding row(s) for %s/%s — cleans to empty",
                    deleted, source, source_id,
                )
                session.flush()
            return False

        content_hash = _content_hash(content)

        # Identity is (source, source_id, user_id). The owner is part of the
        # key because source_id is only unique *within* an owner for sources
        # keyed on a natural identifier — two users' vaults both contain
        # `Inbox/note.md`. `filter_by(user_id=None)` renders as `IS NULL`, so
        # household-shared sources keep their existing behaviour exactly.
        existing = (
            session.query(Embedding)
            .filter_by(
                source=source, source_id=source_id,
                user_id=user_id, content_hash=content_hash,
            )
            .first()
        )
        if existing:
            return False  # Already embedded with same content

        # Check if already queued with same content
        queued = (
            session.query(EmbeddingQueue)
            .filter_by(
                source=source, source_id=source_id,
                user_id=user_id, status="pending",
            )
            .first()
        )
        if queued:
            if queued.content_hash == content_hash:
                return False  # Already queued with same content
            # Content changed — update the queue item
            queued.content = content
            queued.content_hash = content_hash
            queued.metadata_json = metadata_json
            queued.user_id = user_id
            queued.created_at = datetime.now(timezone.utc)
            session.flush()
            return True

        # Enqueue new item
        session.add(EmbeddingQueue(
            source=source,
            source_id=source_id,
            user_id=user_id,
            content=content,
            content_hash=content_hash,
            metadata_json=metadata_json,
            status="pending",
        ))
        session.flush()
        return True

    @staticmethod
    def enqueue_batch(
        session: Session,
        items: list[tuple[str, str, str, str | None]],
        user_id: int | None = None,
    ) -> int:
        """Enqueue multiple items efficiently.

        Each item is (source, source_id, content, metadata_json).
        user_id applies to every item in the batch (None = shared).
        Returns count of items actually enqueued (not skipped).

        Raises UnknownEmbeddingSourceError if any item's `source` isn't
        declared by any integration's manifest `embedding_sources`.
        """
        if not items:
            return 0

        for source in {item[0] for item in items}:
            _check_known_source(source)

        # Clean + cap + hash. Must mirror enqueue() exactly, or the same text
        # would dedup differently depending on which entry point queued it.
        prepared = []
        for source, source_id, content, metadata_json in items:
            content = _prepare_content(content, source, metadata_json)
            prepared.append((source, source_id, content, _content_hash(content), metadata_json))

        # Batch-check existing embeddings
        keys = [(s, sid) for s, sid, _, _, _ in prepared]
        existing_embeddings = {}
        for source, source_id in keys:
            row = (
                session.query(Embedding.content_hash)
                .filter_by(source=source, source_id=source_id)
                .first()
            )
            if row:
                existing_embeddings[(source, source_id)] = row.content_hash

        # Batch-check existing queue items
        existing_queue = {}
        for source, source_id in keys:
            row = (
                session.query(EmbeddingQueue.content_hash)
                .filter_by(source=source, source_id=source_id, status="pending")
                .first()
            )
            if row:
                existing_queue[(source, source_id)] = row.content_hash

        count = 0
        for source, source_id, content, content_hash, metadata_json in prepared:
            key = (source, source_id)

            # Skip if already embedded with same content
            if key in existing_embeddings and existing_embeddings[key] == content_hash:
                continue

            # Skip if already queued with same content
            if key in existing_queue and existing_queue[key] == content_hash:
                continue

            session.add(EmbeddingQueue(
                source=source,
                source_id=source_id,
                user_id=user_id,
                content=content,
                content_hash=content_hash,
                metadata_json=metadata_json,
                status="pending",
            ))
            count += 1

        if count:
            session.flush()

        return count

    @staticmethod
    def process_queue(
        session: Session,
        batch_size: int = 100,
        *,
        spaces: str = "primary",
        stats: dict | None = None,
    ) -> int:
        """Process pending items from the embedding queue.

        Fetches up to batch_size pending items, embeds them, and stores
        in the unified embeddings table. Removes old embeddings for items
        being re-embedded (content changed).

        `spaces` (2026-09-07): `"primary"` (default) embeds each item into
        `get_providers()[0]`'s space only — see the module docstring's point
        1 for why the live worker no longer pays for every active space on
        every cycle. `"all"` restores the previous behaviour (every active
        space written inline) for callers that need it, e.g. `reembed.py`'s
        post-reclean drain. In `"primary"` mode, a primary-space failure
        falls back to the next available space for that batch (module
        docstring point 2) rather than deferring the item; pass a `stats`
        dict to have `{"fallback": <items using a fallback space>}` recorded
        into it (merged, not replaced, across recursive bisect calls).

        **Single-flight across processes, via a Postgres advisory lock.** A
        second caller returns 0 immediately rather than working in parallel.
        This is not just tidiness: two concurrent fastembed subprocesses take
        the 7.8 GB server to ~350 MB available and it thrashes to a standstill,
        with Postgres on the same box. Observed twice.

        The old comment here claimed the function was already single-flight
        "(scheduler runs it with max_instances=1)". That only ever held against
        the scheduler itself — `max_instances` is a property of one APScheduler
        job, not of this function — so the moment a manual drain existed there
        was no mutual exclusion at all. A session-level (not xact-level) lock,
        because this function commits several times and an xact lock would drop
        at the first one.

        Returns number of items processed, or 0 if another process holds the lock.

        The lock is **transaction-scoped on a dedicated connection**, which is
        not fussiness. A session-level lock taken on `session` leaks: this
        function commits several times, a SQLAlchemy session may return its
        connection to the pool between transactions, and `pg_advisory_unlock`
        on a *different* connection than the one holding the lock silently
        returns false — leaving the lock stuck on a pooled connection forever.
        That failure showed up as nine unrelated tests failing in the full suite
        while every one of them passed alone.

        `pg_try_advisory_xact_lock` is released when its transaction ends, so
        rollback-and-close guarantees it, and process death guarantees it too.
        The cost is one connection sitting idle-in-transaction, holding nothing
        but the lock, while `session` commits independently.
        """
        engine = session.get_bind()
        lock_conn = engine.connect()
        try:
            trans = lock_conn.begin()
            try:
                got = lock_conn.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _QUEUE_LOCK_KEY}
                ).scalar()
                if not got:
                    logger.info("embedding queue is being processed elsewhere — skipping")
                    return 0
                return EmbeddingService._process_queue_locked(
                    session, batch_size, spaces=spaces, stats=stats
                )
            finally:
                trans.rollback()
        finally:
            lock_conn.close()

    @staticmethod
    def _process_queue_locked(
        session: Session, batch_size: int, *, spaces: str = "primary", stats: dict | None = None
    ) -> int:
        # Reclaim orphans: the advisory lock above makes this genuinely
        # single-flight, so anything still 'processing' at entry was abandoned
        # by a killed run — put it back in the queue.
        orphaned = (
            session.query(EmbeddingQueue)
            .filter_by(status="processing")
            .update({"status": "pending"}, synchronize_session=False)
        )
        if orphaned:
            logger.warning(f"Reclaimed {orphaned} orphaned 'processing' queue items")
            session.commit()

        # Fetch pending items
        pending = (
            session.query(EmbeddingQueue)
            .filter_by(status="pending")
            .order_by(EmbeddingQueue.created_at)
            .limit(batch_size)
            .all()
        )

        if not pending:
            return 0

        # Mark as processing
        for item in pending:
            item.status = "processing"
        session.flush()

        t0 = time.time()
        done = EmbeddingService._embed_and_store(session, pending, spaces=spaces, stats=stats)
        elapsed = time.time() - t0
        logger.info(
            f"Embedded {done}/{len(pending)} items in {elapsed:.1f}s (subprocess)"
        )

        # Clean up old done/error items (keep last 24h for debugging)
        from sqlalchemy import and_
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        session.query(EmbeddingQueue).filter(
            and_(
                EmbeddingQueue.status.in_(["done", "error"]),
                EmbeddingQueue.processed_at < cutoff,
            )
        ).delete(synchronize_session=False)
        session.commit()

        return done

    @staticmethod
    def _embed_and_store(
        session: Session,
        items: list["EmbeddingQueue"],
        *,
        spaces: str = "primary",
        stats: dict | None = None,
    ) -> int:
        """Embed a batch and store; bisect on total failure.

        Previously one bad item (or a transient subprocess failure) marked
        the whole batch 'error' with no retry. Now: a failed batch splits in
        half and each half retries, isolating the poison item. Attempts only
        increment at the single-item leaf — penalising the 99 innocent
        cohort members would error them out alongside the real culprit.

        `spaces="all"` embeds into every active space, independently, and an
        item is 'done' once *any* space has it (partial success across
        spaces is deliberate — see the module docstring's point 3).
        `spaces="primary"` (default, 2026-09-07) embeds into only
        `get_providers()[0]`'s space, falling back to the next available
        space if the primary raises, so a batch still becomes searchable
        during a primary-provider outage — see the module docstring's point
        2. Either way, only a batch where *every* attempted space failed
        falls through to the bisect/retry path below.

        Returns the number of items successfully embedded.
        """
        texts = [item.content for item in items]
        active = _active_spaces()
        if not active:
            raise RuntimeError(
                "no embedding provider is available — check embedding_provider "
                "and any required API keys"
            )

        candidates = active if spaces == "all" else active[:1]

        vectors_by_space: dict[str, list[list[float]]] = {}
        last_exc: Exception | None = None
        rate_limited = False
        for provider, _vec_model in candidates:
            try:
                vectors_by_space[provider.provider_id] = _embed_with(provider, texts)
            except GeminiRateLimitError as e:
                # A 429 is a per-minute quota, not an outage — never falls
                # back to the CPU model for it (2026-09-07 incident: doing
                # so made the run slower than simply waiting). The item
                # stays 'pending' for the next tick via the bisect/retry
                # path below, same as any other failed batch.
                last_exc = e
                rate_limited = True
                logger.warning(
                    "embedding space %s is rate-limited for a batch of %d — "
                    "leaving pending for the next tick rather than falling "
                    "back to a slower space: %s",
                    provider.provider_id, len(items), str(e)[:200],
                )
            except Exception as e:
                last_exc = e
                logger.warning(
                    "embedding space %s failed for a batch of %d (%s: %s) — "
                    "other spaces continue; the gap is backfillable",
                    provider.provider_id, len(items), type(e).__name__, str(e)[:200],
                )

        # Primary-mode fallback: the primary space failed but another active
        # space exists — use it so the batch is searchable today rather than
        # waiting on the nightly `embedding_space_backfill` to fill the
        # primary. One successful fallback space is enough; the point is
        # "searchable now", not "every space now". Never for a rate limit
        # (`rate_limited`) — see the exception handler above.
        if spaces != "all" and not vectors_by_space and len(active) > 1 and not rate_limited:
            primary_id = active[0][0].provider_id
            for provider, _vec_model in active[1:]:
                try:
                    vectors_by_space[provider.provider_id] = _embed_with(provider, texts)
                except Exception as e:
                    last_exc = e
                    logger.warning(
                        "fallback embedding space %s also failed for a batch of "
                        "%d (%s: %s)",
                        provider.provider_id, len(items), type(e).__name__, str(e)[:200],
                    )
                    continue
                logger.warning(
                    "embedding: primary space %s unavailable for a batch of %d "
                    "— fell back to %s; the nightly space backfill will fill "
                    "the primary gap",
                    primary_id, len(items), provider.provider_id,
                )
                if stats is not None:
                    stats["fallback"] = stats.get("fallback", 0) + len(items)
                break

        if not vectors_by_space:
            e = last_exc or RuntimeError("all embedding spaces failed")
            if len(items) == 1:
                item = items[0]
                item.attempts = (item.attempts or 0) + 1
                item.error_message = str(e)[:500]
                if item.attempts >= MAX_EMBED_ATTEMPTS:
                    item.status = "error"
                    logger.error(
                        f"Embedding gave up on {item.source}:{item.source_id} "
                        f"after {item.attempts} attempts: {e}"
                    )
                else:
                    item.status = "pending"  # retried next cycle
                session.commit()
                return 0
            mid = len(items) // 2
            logger.warning(
                f"Embedding batch of {len(items)} failed ({type(e).__name__}) "
                f"— bisecting to isolate"
            )
            return (
                EmbeddingService._embed_and_store(
                    session, items[:mid], spaces=spaces, stats=stats
                )
                + EmbeddingService._embed_and_store(
                    session, items[mid:], spaces=spaces, stats=stats
                )
            )

        now = datetime.now(timezone.utc)

        # Drop superseded rows first, as their own pass. The per-space vector
        # rows go with them via ON DELETE CASCADE, so there is no per-space
        # cleanup to forget when a third space is added. Done before any insert
        # so an autoflush can't delete a row this same batch just wrote.
        #
        # P5 (hardening-2026-08.md): one statement for the whole batch via a
        # composite tuple_().in_() rather than one DELETE per item — the
        # autoflush-ordering reason above is about *when* this pass runs
        # relative to the insert pass, not how many statements it takes, so
        # batching doesn't disturb it.
        #
        # ⚠️ Keyed on (source, source_id, user_id), the same identity `enqueue`
        # dedups on: two users' vaults both contain `Inbox/note.md`, so a key
        # of (source, source_id) alone made re-embedding one person's note
        # delete the other's row (found by the 2026-09-06 scoping audit). The
        # household-shared rows have user_id NULL, and SQL NULL never equals
        # NULL, so they get their own statement with an explicit `IS NULL`
        # rather than a third tuple member that would match nothing.
        owned = [(i.source, i.source_id, i.user_id) for i in items if i.user_id is not None]
        shared = [(i.source, i.source_id) for i in items if i.user_id is None]
        if owned:
            session.query(Embedding).filter(
                tuple_(Embedding.source, Embedding.source_id, Embedding.user_id).in_(owned)
            ).delete(synchronize_session=False)
        if shared:
            session.query(Embedding).filter(
                Embedding.user_id.is_(None),
                tuple_(Embedding.source, Embedding.source_id).in_(shared),
            ).delete(synchronize_session=False)
        session.flush()

        new_rows: list[Embedding] = []
        for item in items:
            row = Embedding(
                source=item.source,
                source_id=item.source_id,
                user_id=item.user_id,
                chunk_index=0,
                chunk_text=item.content,
                content_hash=item.content_hash,
                metadata_json=item.metadata_json,
                cleaner_version=CLEANER_VERSION,
                created_at=now,
            )
            session.add(row)
            new_rows.append(row)
        session.flush()  # assigns row.id, which the vector rows key on

        for provider, vec_model in active:
            vectors = vectors_by_space.get(provider.provider_id)
            if vectors is None:
                continue  # this space wasn't attempted or failed; backfill will close it
            # strict: see the matching note in backfill.fill_space — a provider
            # returning fewer vectors than rows must be loud, not a partial write.
            for row, vec in zip(new_rows, vectors, strict=True):
                session.add(vec_model(
                    embedding_id=row.id,
                    embedding=vec,
                    model_name=provider.model_name,
                    created_at=now,
                ))

        for item in items:
            item.status = "done"
            item.processed_at = now

        session.commit()
        return len(items)

    @staticmethod
    def search(
        session: Session,
        query: str,
        sources: list[str] | None = None,
        limit: int = 10,
        source_filter: str | None = None,
        extra_filter: Any = None,
        apply_recency_decay: bool = True,
    ) -> list[dict]:
        """Semantic search across the unified embeddings table.

        Args:
            query: Natural language search query.
            sources: List of sources to search (e.g. ["vault", "email"]).
                     None means all sources.
            limit: Max results.
            source_filter: Additional filter on source_id (e.g. folder path prefix).
            extra_filter: An optional SQLAlchemy boolean clause (built by the
                caller against `Embedding` columns/expressions, e.g. a date-range
                predicate) ANDed into the WHERE before the ORDER BY/LIMIT below.
                Composing it here — not filtering `search()`'s return value —
                is what makes a narrow date range return the best K rows *within*
                that range rather than silently fewer than K (or zero) after a
                top-K vector search already picked the wrong K.
            apply_recency_decay: R4 (2026-09-04) — multiply cosine similarity
                by a recency decay (plus an extra fixed penalty for chunks a
                producer has flagged `is_history`) before ranking/limiting, so
                a live file beats its own stale snapshot by construction. On
                by default — see the module comment above `_HISTORY_PENALTY`
                for the mechanism. Callers pass `False` when recency is the
                wrong signal for what they're comparing: `historical_corpus`'s
                handlers (an archive, where "old" isn't "worse") and
                `coffee`'s similarity search (flavor-profile matching, where a
                perfect match logged a year ago must not lose to a middling
                one logged today).

        Results are user-scoped: rows owned by the bound user plus
        household-shared rows (user_id IS NULL). Unbound callers
        (background jobs) see shared rows only.

        Answered from the first configured space that is available and has any
        coverage; each result carries a `space` naming the model that produced
        it. **That field is not decoration.** A fallback is not a transparent
        substitute — measured 10-NN agreement between the two spaces is under
        50%, so a silent fallback reads as the primary model quietly getting
        worse. Whoever renders these results should be able to say which index
        answered.

        The query is embedded into exactly one space and scored only against
        that space's vectors. Cosine distance across models is meaningless;
        there is deliberately no path here that mixes them.

        Returns list of dicts with: source, source_id, score, preview, metadata,
        space, source_date, is_history, stale. The last three are provenance
        (R4) — always present regardless of `apply_recency_decay`, which only
        controls whether they also affect `score`.

        Raises UnknownEmbeddingSourceError if any entry in `sources` isn't
        declared by any integration's manifest `embedding_sources`.
        """
        if sources:
            for source in sources:
                _check_known_source(source)

        # Folder-scoped vault grant (2026-09-06): a folder restriction is a
        # predicate on vault paths, so under one this search must be vault-only
        # — refused otherwise, never run unrestricted. `restrict()` below then
        # applies the grant clause together with the caller's `source_filter`.
        vault_scope.require_vault_only(sources, what="semantic search")

        uid = current_user_id_or_none()
        last_exc: Exception | None = None
        # Read once even when apply_recency_decay=False: `stale` is a label,
        # independent of whether decay also affects `score` (see docstring).
        half_life_days, staleness_threshold_days = _recency_settings()

        for provider, vec_model in _active_spaces():
            # Skip a space with no vectors at all — a newly-configured provider
            # that hasn't been backfilled yet would otherwise answer every
            # query with nothing and never fall through. EXISTS, not COUNT:
            # this runs on every search.
            if session.query(vec_model.embedding_id).limit(1).first() is None:
                continue

            _enable_iterative_scan(session)
            try:
                q_vec = _embed_query_with(provider, query)
            except Exception as e:
                last_exc = e
                logger.warning(
                    "embedding query failed in space %s (%s: %s) — trying next",
                    provider.provider_id, type(e).__name__, str(e)[:200],
                )
                continue

            q = session.query(
                Embedding.source,
                Embedding.source_id,
                Embedding.chunk_text,
                Embedding.metadata_json,
                Embedding.created_at,
                vec_model.embedding.cosine_distance(q_vec).label("distance"),
            ).join(vec_model, vec_model.embedding_id == Embedding.id)

            if uid is not None:
                q = q.filter(or_(Embedding.user_id.is_(None), Embedding.user_id == uid))
            else:
                q = q.filter(Embedding.user_id.is_(None))

            if sources:
                q = q.filter(Embedding.source.in_(sources))

            # The caller's prefix filter and, when bound, the grant's folder
            # scope — one call, so neither can be applied without the other.
            q = vault_scope.restrict(q, Embedding.source_id, source_filter)

            if extra_filter is not None:
                q = q.filter(extra_filter)

            # Overfetch when decay can reorder the candidate set — a chunk
            # ranked #30 by raw cosine distance can rank above the raw #1 once
            # a stale/is_history penalty is applied, so the SQL ORDER BY/LIMIT
            # can no longer be the final word; it just picks the candidate
            # pool decay then re-ranks in Python. Same overfetch shape
            # `near_duplicates` already uses. Decay off keeps the exact prior
            # single SQL ORDER BY/LIMIT behavior.
            fetch_limit = min(max(limit * 4, limit), 200) if apply_recency_decay else min(limit, 50)
            q = q.order_by("distance").limit(fetch_limit)

            results = []
            for row in q.all():
                prov = _chunk_provenance(row.metadata_json, row.created_at, staleness_threshold_days)
                raw_score = 1 - row.distance
                if apply_recency_decay:
                    decay = _decay_factor(prov["age_days"], half_life_days)
                    if prov["is_history"]:
                        decay *= _HISTORY_PENALTY
                    score = round(raw_score * decay, 4)
                else:
                    score = round(raw_score, 4)
                results.append({
                    "source": row.source,
                    "source_id": row.source_id,
                    "score": score,
                    "preview": row.chunk_text[:300] if row.chunk_text else "",
                    "metadata": row.metadata_json,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "space": provider.model_name,
                    "source_date": prov["source_date"],
                    "is_history": prov["is_history"],
                    "stale": prov["stale"],
                })

            if apply_recency_decay:
                results.sort(key=lambda r: r["score"], reverse=True)
            return results[:limit]

        if last_exc is not None:
            raise last_exc
        return []

    @staticmethod
    def similar_to(
        session: Session,
        source: str,
        source_id: str,
        limit: int = 10,
        source_filter: str | None = None,
        min_score: float = 0.0,
    ) -> list[dict]:
        """More-like-this, starting from a vector already in Postgres.

        **This costs nothing.** The one paid, network-dependent, offline-breaking
        step in the whole similarity surface is `text -> vector`; anything that
        starts from a stored vector is linear algebra inside the database. That
        is the structural rule the Gemini migration was designed around, and
        this is the first surface built on it.

        Aggregates per `source_id`: a note that chunked into twelve rows should
        not occupy twelve result slots, and the *best-matching* chunk is the
        honest score for "how alike are these two notes". The item itself is
        excluded — including every one of its own sibling chunks, which is the
        part a naive `!= source_id` on the chunk id gets wrong.

        Answers from the same space the seed vector lives in and never mixes
        spaces, for the reason `search()` documents at length: cosine distance
        across models is meaningless. If a seed has vectors in more than one
        space, the first configured space wins, matching `search()`.

        Returns the same dict shape as `search()`, so callers can render either.
        """
        vault_scope.require_vault_only([source], what="similarity search")
        uid = current_user_id_or_none()

        # A chunked document's rows carry `{path}#{n}` source_ids, not the bare
        # path (see obsidian/chunking.py). Everything below therefore keys on
        # the *document* — `split_part(source_id, '#', 1)` — so that a seed
        # given as either form resolves, and so a note's own sibling chunks are
        # excluded from its results rather than filling them.
        doc = source_id.split("#", 1)[0]
        doc_key = func.split_part(Embedding.source_id, "#", 1)

        for provider, vec_model in _active_spaces():
            _enable_iterative_scan(session)
            # The seed's own vectors in this space. A seed with no vector here
            # (queued but not yet embedded, or embedded only into another
            # space) is not an error — try the next space. Under a folder
            # grant the seed itself must be inside the grant — a seed outside
            # it reads as "no vectors", so a grantee cannot use a private note
            # as a probe by naming its path.
            seed_q = (
                session.query(Embedding.id)
                .join(vec_model, vec_model.embedding_id == Embedding.id)
                .filter(Embedding.source == source, doc_key == doc)
            )
            seed_ids = [row[0] for row in vault_scope.restrict(seed_q, Embedding.source_id).all()]
            if not seed_ids:
                continue

            seed_vecs = [
                row[0]
                for row in session.query(vec_model.embedding)
                .filter(vec_model.embedding_id.in_(seed_ids))
                .all()
            ]

            best: dict[str, dict] = {}
            for seed_vec in seed_vecs:
                q = session.query(
                    Embedding.source,
                    Embedding.source_id,
                    Embedding.chunk_text,
                    Embedding.metadata_json,
                    Embedding.created_at,
                    vec_model.embedding.cosine_distance(seed_vec).label("distance"),
                ).join(vec_model, vec_model.embedding_id == Embedding.id)

                if uid is not None:
                    q = q.filter(
                        or_(Embedding.user_id.is_(None), Embedding.user_id == uid)
                    )
                else:
                    q = q.filter(Embedding.user_id.is_(None))

                q = q.filter(Embedding.source == source)
                # Exclude the seed *document*, not merely the seed chunk.
                q = q.filter(doc_key != doc)
                q = vault_scope.restrict(q, Embedding.source_id, source_filter)

                # Over-fetch: several seed chunks will agree on the same
                # neighbours, so per-chunk top-K collapses to fewer than K
                # documents after aggregation.
                for row in q.order_by("distance").limit(min(limit * 5, 200)).all():
                    score = round(1 - row.distance, 4)
                    if score < min_score:
                        continue
                    hit_doc = row.source_id.split("#", 1)[0]
                    prev = best.get(hit_doc)
                    if prev is None or score > prev["score"]:
                        best[hit_doc] = {
                            "source": row.source,
                            "source_id": hit_doc,
                            "score": score,
                            "preview": row.chunk_text[:300] if row.chunk_text else "",
                            "metadata": row.metadata_json,
                            "created_at": (
                                row.created_at.isoformat() if row.created_at else None
                            ),
                            "space": provider.model_name,
                        }

            return sorted(best.values(), key=lambda r: r["score"], reverse=True)[:limit]

        return []

    @staticmethod
    def near_duplicates(
        session: Session,
        source: str,
        threshold: float = 0.95,
        source_filter: str | None = None,
        limit: int = 50,
        exclude_prefixes: list[str] | None = None,
    ) -> list[dict]:
        """Find pairs of near-identical documents. Also free — no API calls.

        Maintenance tool, not a hot path: it walks one representative chunk per
        document and asks for that chunk's nearest neighbours. Deliberately
        *not* a single self-join over every chunk pair — that is O(n^2) over
        ~5.6k vault chunks (~16M pairs) and the honest version needs a LATERAL
        query this codebase has no other example of. One query per document is
        a few hundred round trips against an HNSW index, which is fine for
        something run occasionally and easy to reason about.

        The representative is the document's **longest** chunk, because a real
        duplicate matches on any chunk while a short one (a heading stub, a
        stray line) matches on noise and invents pairs.

        Pairs are unordered and de-duplicated: (a,b) and (b,a) are one row.

        `threshold` of 0.95 is the migration plan's suggested near-duplicate
        line. Do not read a score here as "these are the same file" — it means
        "these are worth a human look", which for Syncthing conflict twins,
        copy-paste forks and superseded drafts is exactly the question.

        `exclude_prefixes` drops whole path prefixes from *both* sides of the
        comparison. It exists because template-generated notes defeat this
        method: the first unscoped run over the real vault returned 60 pairs of
        which **45 were `Daily Notes/` matching other `Daily Notes/`** at
        0.983-0.988. Those notes share a frontmatter block, a heading skeleton
        and two Obsidian task queries, so a mostly-empty Tuesday is genuinely
        near-identical to a mostly-empty Wednesday — the score is honest and
        completely useless, and it buried every real finding below it.

        The general rule this encodes: **similarity finds duplicates only among
        documents whose *form* varies.** Where form is fixed by a template,
        the measure collapses. Caller passes `[]` to compare everything.
        """
        vault_scope.require_vault_only([source], what="duplicate detection")
        uid = current_user_id_or_none()
        if exclude_prefixes is None:
            exclude_prefixes = list(DEFAULT_DUPLICATE_EXCLUDES)

        for provider, vec_model in _active_spaces():
            rows = (
                session.query(
                    func.split_part(Embedding.source_id, "#", 1).label("doc"),
                    Embedding.id,
                    func.length(Embedding.chunk_text).label("n"),
                )
                .join(vec_model, vec_model.embedding_id == Embedding.id)
                .filter(Embedding.source == source)
            )
            if uid is not None:
                rows = rows.filter(
                    or_(Embedding.user_id.is_(None), Embedding.user_id == uid)
                )
            else:
                rows = rows.filter(Embedding.user_id.is_(None))
            rows = vault_scope.restrict(rows, Embedding.source_id, source_filter)
            for pref in exclude_prefixes:
                rows = rows.filter(~Embedding.source_id.ilike(f"{pref}%"))

            longest: dict[str, tuple[int, int]] = {}
            for source_id, emb_id, n in rows.all():
                cur = longest.get(source_id)
                if cur is None or (n or 0) > cur[1]:
                    longest[source_id] = (emb_id, n or 0)
            if not longest:
                continue

            seen: dict[tuple[str, str], float] = {}
            for source_id in longest:
                for hit in EmbeddingService.similar_to(
                    session, source, source_id,
                    limit=5, source_filter=source_filter, min_score=threshold,
                ):
                    # Exclusions must apply to the *neighbour* too, not just the
                    # seed: filtering only the seed side still lets a templated
                    # note be returned as everyone else's nearest match.
                    if any(hit["source_id"].startswith(p) for p in exclude_prefixes):
                        continue
                    key = tuple(sorted((source_id, hit["source_id"])))
                    if hit["score"] > seen.get(key, 0.0):
                        seen[key] = hit["score"]

            pairs = [
                {"a": a, "b": b, "score": s, "space": provider.model_name}
                for (a, b), s in seen.items()
            ]
            return sorted(pairs, key=lambda p: p["score"], reverse=True)[:limit]

        return []

    @staticmethod
    def delete_source(
        session: Session,
        source: str,
        source_id: str,
        user_id: int | None = None,
        *,
        scope_user: bool = False,
    ) -> int:
        """Remove embeddings for a source item (e.g. deleted vault file).

        Pass `scope_user=True` (with `user_id`) for sources whose `source_id`
        is only unique within an owner — deleting one user's `Inbox/note.md`
        must not delete the other user's file of the same name. Callers that
        omit it keep the old cross-owner behaviour, which is correct for
        globally-unique ids (Gmail message ids, WhatsApp message keys).

        Raises UnknownEmbeddingSourceError if `source` isn't declared by any
        integration's manifest `embedding_sources`.
        """
        _check_known_source(source)
        emb_q = session.query(Embedding).filter_by(source=source, source_id=source_id)
        queue_q = session.query(EmbeddingQueue).filter_by(
            source=source, source_id=source_id, status="pending"
        )
        if scope_user:
            # filter_by (not `== user_id`) so a None owner renders as IS NULL
            # rather than `= NULL`, which matches nothing.
            emb_q = emb_q.filter_by(user_id=user_id)
            queue_q = queue_q.filter_by(user_id=user_id)

        count = emb_q.delete(synchronize_session=False)
        queue_q.delete(synchronize_session=False)
        session.flush()
        return count

    @staticmethod
    def stats(session: Session) -> dict:
        """Return embedding pipeline statistics."""
        total = session.query(func.count(Embedding.id)).scalar() or 0
        by_source = dict(
            session.query(Embedding.source, func.count(Embedding.id))
            .group_by(Embedding.source)
            .all()
        )
        # Per-space coverage. This is the gauge for both a provider swap and
        # Phase 4's backfill: `missing` is the size of the anti-join that
        # closes the gap, and a space sitting at 0 is a configured provider
        # that has never been backfilled. `by_model` within a space catches
        # the other case — a model upgraded in place at the same width.
        spaces = []
        for provider_id, vec_model in VECTOR_MODELS.items():
            covered = session.query(func.count(vec_model.embedding_id)).scalar() or 0
            spaces.append({
                "provider": provider_id,
                "vectors": covered,
                "missing": total - covered,
                "by_model": dict(
                    session.query(vec_model.model_name, func.count(vec_model.embedding_id))
                    .group_by(vec_model.model_name)
                    .all()
                ),
                "active": provider_id in {p.provider_id for p, _ in _active_spaces()},
            })
        # Text-preparation coverage, the same idea as per-space coverage above:
        # rows whose cleaner_version isn't current were built from differently
        # prepared text, and that was previously invisible. NULL = predates the
        # column entirely. `stale` is the other half of Phase 4's worklist.
        by_cleaner = {
            (str(v) if v is not None else "unknown"): n
            for v, n in session.query(
                Embedding.cleaner_version, func.count(Embedding.id)
            ).group_by(Embedding.cleaner_version).all()
        }
        cleaning = {
            "current_version": CLEANER_VERSION,
            "by_version": by_cleaner,
            "stale": total - by_cleaner.get(str(CLEANER_VERSION), 0),
        }
        queue_pending = (
            session.query(func.count(EmbeddingQueue.id))
            .filter_by(status="pending")
            .scalar() or 0
        )
        queue_error = (
            session.query(func.count(EmbeddingQueue.id))
            .filter_by(status="error")
            .scalar() or 0
        )
        latest = session.query(func.max(Embedding.created_at)).scalar()

        return {
            "model": MODEL_NAME,
            "dimensions": VECTOR_DIM,
            "total_embeddings": total,
            "by_source": by_source,
            "spaces": spaces,
            "cleaning": cleaning,
            "queue_pending": queue_pending,
            "queue_errors": queue_error,
            "last_embedded": latest.isoformat() if latest else None,
        }
