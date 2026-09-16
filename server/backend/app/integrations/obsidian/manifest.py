from app.plugin.manifest import IntegrationManifest, TaskSpec

MANIFEST = IntegrationManifest(
    name="obsidian",
    display_name="Obsidian Vault",
    version="1.0.0",
    type="source",
    description="Per-user vault file indexing with pgvector semantic search; server-side watcher backstops Syncthing.",
    icon="BookOpen",
    models=["VaultChunk"],
    embedding_sources=["vault"],
    reads_from=[],
    writes_to=[],
    schedule="*/30 * * * *",  # matches Syncthing settle time
    schedule_timezone=None,
    freshness_threshold_minutes=10,  # services/freshness.py: 600s
    staleness_probe=None,  # not in data_freshness.py's DATA_FRESHNESS_THRESHOLDS
    background_tasks=[
        TaskSpec(
            name="obsidian_vault_watcher",
            target="app.integrations.obsidian.watcher:run_watcher_task",
            kind="startup",
        ),
    ],
    routes=[],
    # obsidian_vault_path / vaults_root_path / syncthing_url / syncthing_api_key
    # all stay kernel settings: the Syncthing sidecar is server-wide
    # infrastructure (also used directly by the kernel's own
    # /api/v1/syncthing/* pairing routes, not just this integration's
    # vault_transfer tool), not this integration's own config.
    config_schema={},
    oauth=None,
    provides=["vault.query"],  # facade: app.integrations.obsidian.facade — consumed by system
    depends_on=[],
)
