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
    },
    oauth=None,
    depends_on=[],
)
