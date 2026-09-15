from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest

MANIFEST = IntegrationManifest(
    name="historical_corpus",
    display_name="Historical Corpus",
    version="1.0.0",
    type="capability",
    description="Ingested household documents (scans, PDFs, attachments, equipment manuals) with pgvector semantic search.",
    icon="Archive",
    models=["HistoricalDocument", "HistoricalDocumentChunk"],
    embedding_sources=["historical_corpus"],
    reads_from=[],
    writes_to=[],
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={
        # Which project a document belongs to when the caller doesn't say.
        # Deployment-specific: this used to be the hardcoded literal
        # "riverside" (a family renovation project) scattered across ingest.py
        # and a column server_default. Existing rows keep whatever tag they
        # were ingested with — those are a true contemporaneous record and
        # are never rewritten.
        "default_project_tag": ConfigFieldSpec(
            type="str", default="household",
            description=(
                "Project tag applied to ingested documents when the caller "
                "doesn't specify one, e.g. a renovation or house-purchase "
                "project name."
            ),
        ),
    },
    oauth=None,
    # facade: app.integrations.historical_corpus.facade — consumed by
    # attachments (WhatsApp/email attachment ingest) and inbox (vault Inbox
    # routing).
    provides=["corpus.ingest"],
    depends_on=[],
)
