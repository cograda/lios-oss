from app.plugin.manifest import IntegrationManifest

MANIFEST = IntegrationManifest(
    name="coffee",
    display_name="Coffee",
    version="1.0.0",
    type="capability",
    description="Coffee bag/brew log with dial-in advice and semantic search over brew notes.",
    icon="Coffee",
    models=["Coffee", "CoffeeBrew", "CoffeeEquipmentProfile"],
    embedding_sources=["coffee"],
    reads_from=[],
    writes_to=[],
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={},
    oauth=None,
    provides=["coffee.query"],  # facade: app.integrations.coffee.facade — consumed by system
    depends_on=[],
)
