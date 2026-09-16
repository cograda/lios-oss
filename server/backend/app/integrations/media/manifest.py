from app.plugin.manifest import IntegrationManifest

MANIFEST = IntegrationManifest(
    name="media",
    display_name="Media Store",
    version="1.0.0",
    type="source",
    description="Indexes and downloads WhatsApp media (images/video/audio) from the Baileys bridge.",
    icon="Image",
    models=["MediaItem"],
    embedding_sources=[],
    reads_from=["whatsapp-bridge"],
    writes_to=[],
    schedule="5,35 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    # media_root is a Docker volume mount path (infra concern, like the
    # vault paths) — kernel setting, not this integration's own config.
    config_schema={},
    oauth=None,
    provides=["media.store"],  # facade: app.integrations.media.facade — consumed by snags
    depends_on=[],
)
