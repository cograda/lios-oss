from app.plugin.manifest import IntegrationManifest

MANIFEST = IntegrationManifest(
    name="attachments",
    display_name="Message Attachments",
    version="1.0.0",
    type="capability",
    description="Ingests email attachments into the historical corpus (download, parse, embed).",
    icon="Paperclip",
    models=["MessageAttachment"],
    embedding_sources=[],  # feeds historical_corpus's embedding source, not its own
    reads_from=["gmail-api"],
    writes_to=[],
    schedule="*/30 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={},
    oauth=None,
    provides=["attachments.query"],  # facade: app.integrations.attachments.facade — consumed by system
    depends_on=["mail.query", "corpus.ingest"],
)
