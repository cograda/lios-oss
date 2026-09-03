from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest

MANIFEST = IntegrationManifest(
    name="irish_rail",
    display_name="Irish Rail",
    version="1.0.0",
    type="action",
    description="Live Irish Rail station departure board — no local caching.",
    icon="Train",
    models=[],  # no models.py — live API, nothing cached
    embedding_sources=[],
    reads_from=["irish-rail-api"],
    writes_to=[],
    schedule=None,  # live API, no scheduled sync
    schedule_timezone=None,
    freshness_threshold_minutes=None,  # explicit "never" in services/freshness.py
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    # Station is deployment config, not a code constant (2026-07-28).
    # Deliberately NOT `required`: `station` is already a per-request tool
    # argument, so the tools remain usable without config. Only the *default*
    # needs it, and the handlers return a message naming the fix. Gating the
    # integration off would remove tools that work fine when given a station.
    config_schema={
        "rail_station_code": ConfigFieldSpec(
            type="str", default="",
            description=(
                "Irish Rail station code used when a caller doesn't pass "
                "`station`, e.g. GSTNS."
            ),
        ),
        "rail_station_name": ConfigFieldSpec(
            type="str", default="",
            description="Human-readable station name, used only in output.",
        ),
    },
    oauth=None,
    provides=["rail.query"],  # facade: app.integrations.irish_rail.facade — consumed by system
    depends_on=[],
)
