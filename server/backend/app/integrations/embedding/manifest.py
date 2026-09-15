from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, TaskSpec

MANIFEST = IntegrationManifest(
    name="embedding",
    display_name="Embedding",
    version="1.0.0",
    type="capability",
    description=(
        "Unified cross-source semantic search — queue, worker, and pgvector "
        "search shared by every embedding-producing integration (vault, "
        "email, WhatsApp, historical corpus, coffee)."
    ),
    icon="Search",
    models=[
        "Embedding",
        "EmbeddingQueue",
        # One per vector space (Phase 2). A new space adds a class here and a
        # migration — forgetting this entry fails boot validation rather than
        # silently omitting the table from create_tables()/Alembic.
        "EmbeddingVecGemini1536",
        "EmbeddingVecBgeSmall384",
    ],
    # This package doesn't own a source itself — it's the shared plumbing
    # every other integration's declared `embedding_sources` feed into via
    # `app.services.embedding.EmbeddingService`.
    embedding_sources=[],
    reads_from=[],
    writes_to=[],
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[
        TaskSpec(
            name="embedding_processor",
            target="app.integrations.embedding.tasks:run_embedding_processor",
            kind="cron",
            cron="*/5 * * * *",
        ),
        # 2026-09-07: the processor above now writes only the primary space
        # (`process_queue`'s `spaces="primary"` default) — see
        # `app.services.embedding`'s module docstring point 1. This nightly
        # job is the other half: walk every other active space's anti-join
        # (`app.integrations.embedding.backfill.fill_space`, the same walker
        # `fill-space` the CLI already used to turn a new provider on) and
        # close the gap. Offset well clear of the `*/5` processor and any
        # other nightly maintenance.
        TaskSpec(
            name="embedding_space_backfill",
            target="app.integrations.embedding.tasks:run_embedding_space_backfill",
            kind="cron",
            cron="30 3 * * *",
        ),
    ],
    routes=[],
    config_schema={
        "gemini_api_key": ConfigFieldSpec(
            type="str",
            # Not required: the whole pipeline runs on the local fastembed
            # provider without it, and marking it required would gate the
            # embedding integration — and therefore all semantic search —
            # off entirely. GeminiEmbeddingProvider raises at its own call
            # site instead, naming the key.
            required=False,
            secret=True,
            description=(
                "Google API key for gemini-embedding-2 (1536-dim). Only read "
                "when the kernel setting `embedding_provider` selects "
                "'gemini-embedding-2'; the default local fastembed provider "
                "needs no key."
            ),
        ),
        # R4 (2026-09-04): recency decay in EmbeddingService.search(), so a
        # live file beats its own stale snapshot by construction rather than
        # via a hand-maintained exclusion list (see obsidian/sync.py's
        # SKIP_DIRS comment on `.stversions` — this is the treatment the
        # tourniquet was waiting for). A `float` days value, not a constant:
        # the right half-life is a judgment call that will need retuning
        # after it's been watched against real queries, and a code change per
        # retune is exactly the kind of drift this config layer exists to
        # avoid. Not `required` — an unset/invalid value falls back to a
        # hardcoded default in `app.services.embedding._recency_settings()`
        # rather than gating semantic search off entirely.
        "recency_half_life_days": ConfigFieldSpec(
            type="float",
            required=False,
            secret=False,
            default=30.0,
            description=(
                "Half-life in days for the recency decay applied to search "
                "scores: score *= 0.5 ** (age_days / this). Lower = older "
                "content loses rank faster. Callers for whom recency is the "
                "wrong signal (vault_duplicates, historical corpus, "
                "similarity-of-content lookups) opt out entirely rather than "
                "tuning this down."
            ),
        ),
        "staleness_threshold_days": ConfigFieldSpec(
            type="float",
            required=False,
            secret=False,
            default=180.0,
            description=(
                "Age in days beyond which a search result's `stale` flag is "
                "set. Read-time only — it labels a result, it never excludes "
                "one (same reasoning as vault_search's `status` field: a "
                "search that silently drops things produces confidently "
                "wrong 'nothing found' answers)."
            ),
        ),
        # 2026-09-07: the `*/5` cron used to call process_queue exactly once
        # per tick regardless of how fast that call actually ran — a ceiling
        # of 1,200 items/hour when each call measured ~10ms of the server's
        # own time on production. This budget lets one run drain the queue
        # (looping process_queue(batch_size=100) until it returns 0) instead
        # of waiting on the clock. Default 240s stays under the 5-minute
        # cadence so a run cannot span into the next tick — the advisory
        # lock in `process_queue` already makes overlap harmless, but one
        # run occupying the whole interval would starve everything else that
        # cron does. Not `required`: an unset/invalid value falls back to
        # `app.integrations.embedding.tasks.DEFAULT_PROCESSOR_BUDGET_SECONDS`.
        "processor_budget_seconds": ConfigFieldSpec(
            type="int",
            required=False,
            secret=False,
            default=240,
            description=(
                "Wall-clock seconds the */5 embedding_processor cron may "
                "spend draining the queue in one run (batches of 100, one "
                "commit each) before stopping for this tick. Keep under 300 "
                "(the cron cadence) so a run cannot span into the next tick."
            ),
        ),
        # 2026-09-07: companion to `processor_budget_seconds`, for the
        # nightly `embedding_space_backfill` job rather than the */5
        # processor — see that TaskSpec's comment above. A larger budget is
        # fine here: this job runs once a day, off-peak, and its whole
        # purpose is to spend the time the */5 processor no longer does.
        "space_backfill_budget_seconds": ConfigFieldSpec(
            type="int",
            required=False,
            secret=False,
            default=2400,
            description=(
                "Wall-clock seconds the nightly embedding_space_backfill "
                "cron may spend filling non-primary spaces' gaps (batches "
                "of 100) before stopping for this run; the next night "
                "resumes from wherever the anti-join still shows a gap."
            ),
        ),
        # 2026-09-07: a 429/RESOURCE_EXHAUSTED from Gemini (a per-minute
        # burst limit, ~100k tokens for a 100-item mail batch) used to
        # exhaust GeminiEmbeddingProvider's retries and fall back to the
        # local bge-small model — ~1s/item, which made the run slower than
        # simply waiting. A 429 now retries in place (honouring any
        # Retry-After header, else a 15s/30s/60s backoff) up to this
        # ceiling and never falls back to the CPU model for it; the item is
        # left 'pending' for the next `*/5` tick instead. A genuine
        # provider failure (5xx, auth, network) is unaffected — see
        # `GeminiRateLimitError`'s docstring in `app/plugin/
        # embedding_provider.py`. Not `required`: an unset/invalid value
        # falls back to `GeminiEmbeddingProvider.rate_limit_max_wait_seconds`.
        "gemini_rate_limit_max_wait_seconds": ConfigFieldSpec(
            type="int",
            required=False,
            secret=False,
            default=240,
            description=(
                "Ceiling in seconds a single Gemini embed batch keeps "
                "retrying a 429/RESOURCE_EXHAUSTED response before giving "
                "up for this tick — it never falls back to the local "
                "bge-small model for a rate limit, only waits."
            ),
        ),
        # 2026-09-07: companion cap to the 64-item `batch_size` on
        # GeminiEmbeddingProvider — a batch of unusually long documents can
        # burst well past the per-minute quota well before hitting the
        # item-count cap. Chars/4 is the estimator (no tokenizer dependency
        # for a cheap admission check); a single item over budget on its own
        # is still sent alone, never dropped.
        "max_batch_tokens": ConfigFieldSpec(
            type="int",
            required=False,
            secret=False,
            default=60_000,
            description=(
                "Estimated token ceiling (chars/4) for one Gemini embed "
                "request, applied in addition to the existing 64-item "
                "batch_size cap. A single item over this budget on its own "
                "is still sent alone rather than dropped."
            ),
        ),
    },
    oauth=None,
    depends_on=[],
)
