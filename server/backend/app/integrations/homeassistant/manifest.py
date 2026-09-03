from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, StalenessProbe, TaskSpec

MANIFEST = IntegrationManifest(
    name="homeassistant",
    display_name="Home Assistant",
    version="1.0.0",
    type="source",
    description="Real-time Home Assistant entity state via WebSocket, reconciled by a 5-min poll.",
    icon="Home",
    models=["HAEntity", "HAStateChange"],
    embedding_sources=[],
    reads_from=["home-assistant-api"],
    writes_to=[],
    schedule="*/5 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=None,  # not in services/freshness.py FRESHNESS_THRESHOLDS
    staleness_probe=StalenessProbe(
        # WS-listener liveness, not HAEntity.synced_at (see data_freshness.py::_probe
        # for why: the poll unconditionally stamps synced_at every cycle regardless
        # of whether the WS listener is alive).
        probe_function="app.integrations.homeassistant.events:ws_last_event_at",
        threshold_minutes=15,
    ),
    background_tasks=[
        TaskSpec(
            name="ha_event_listener",
            target="app.integrations.homeassistant.events:run_listener_task",
            kind="startup",
        ),
    ],
    routes=[],
    config_schema={
        "ha_url": ConfigFieldSpec(
            type="str", required=True,
            description="Home Assistant base URL, e.g. http://192.168.1.51:8123.",
        ),
        "ha_token": ConfigFieldSpec(
            type="str", required=True, secret=True,
            description="Home Assistant long-lived access token.",
        ),
        "ha_record_numeric_history": ConfigFieldSpec(
            type="bool", default=False,
            description=(
                "Record numeric->numeric transitions for EVERY entity. The "
                "firehose — prefer ha_numeric_history_entities."
            ),
        ),
        # Added with the algo harness. A deriver's features() is called with a
        # historical timestamp during training, so it can only be trained on
        # entities whose numeric history is actually kept — and `ha_entities`
        # (latest state only) is the wrong table to read from a feature
        # builder. This allowlist is how a forecaster asks for the handful of
        # series it needs without turning the flag above on for ~1,700
        # entities.
        "ha_numeric_history_entities": ConfigFieldSpec(
            type="list_str", default=[],
            description=(
                "Entity ids whose numeric->numeric transitions ARE recorded in "
                "ha_state_changes, even when ha_record_numeric_history is off. "
                "Keep this short — one entity is a few hundred rows a day."
            ),
        ),
    },
    oauth=None,
    # facade: app.integrations.homeassistant.facade — consumed by system
    # (home status), commute (publishing sensor.commute_* state), and
    # notifications (homeassistant.notify — mobile-app push sink).
    provides=["homeassistant.entities", "homeassistant.notify"],
    depends_on=[],
)
