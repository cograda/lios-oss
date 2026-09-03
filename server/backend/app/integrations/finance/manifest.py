from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest

MANIFEST = IntegrationManifest(
    name="finance",
    display_name="Finance",
    version="1.0.0",
    type="capability",
    description="CSV bank-statement import, categorisation, and spending analytics (joint household data).",
    icon="Wallet",
    models=[
        "Account",
        "Category",
        "CategorizationRule",
        "Transaction",
        "ImportHistory",
        "AccountFingerprint",
        "MonthlySummary",
    ],
    embedding_sources=[],
    reads_from=[],
    writes_to=[],
    schedule=None,  # manual CSV import only
    schedule_timezone=None,
    freshness_threshold_minutes=None,  # explicit "never" in services/freshness.py
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={
        # Declared but not yet wired to any code path (Enable Banking API
        # integration is a stub) — kept so the config exists ahead of that
        # work rather than living as a dead HomeSettings field.
        "banking_api_key": ConfigFieldSpec(
            type="str", required=False, secret=True,
            description="Enable Banking API key (not yet consumed by any sync path).",
        ),
    },
    oauth=None,
    depends_on=[],
)
