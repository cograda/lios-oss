from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest

MANIFEST = IntegrationManifest(
    name="alerts",
    display_name="Alerts",
    version="1.0.0",
    # "capability", like `signals`: nothing here is polled on a schedule —
    # Alertmanager pushes to `routes.py`'s inlet, and the read side is a
    # plain query, not a sync.
    type="capability",
    description=(
        "Reviewable log of monitoring alerts from Alertmanager's webhook "
        "(lios#230) — a durable ledger of what fired/cleared, so a "
        "kickoff/check-in can surface FYI-severity noise instead of it "
        "buzzing the phone."
    ),
    icon="Bell",
    models=["AlertEvent"],
    embedding_sources=[],
    reads_from=["alertmanager-webhook"],
    writes_to=[],
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=["app.integrations.alerts.routes:router"],
    config_schema={
        # Env fallback for `alerts_inlet_key` is `HOME_ALERTS_INLET_KEY` —
        # plugin_config()'s env fallback is always `HOME_{key.upper()}`,
        # never integration-prefixed beyond what the key name itself
        # carries (see signals/manifest.py's identical note). The deploy/
        # side (docker-compose.yml, .env.example) maps the externally-named
        # `LIOS_ALERTS_INLET_KEY` onto this internal env var — see
        # docker-compose.yml's comment on the `app` service.
        "alerts_inlet_key": ConfigFieldSpec(
            type="str",
            required=False,
            secret=True,
            description=(
                "Shared secret Alertmanager's webhook receiver must present "
                "as `Authorization: Bearer <key>`. Without it the route "
                "accepts nothing (503 on every request) rather than "
                "accepting everything — see routes.py."
            ),
        ),
    },
    oauth=None,
    provides=["alerts.query"],
    depends_on=[],
)
