from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, OAuthRequirement

MANIFEST = IntegrationManifest(
    name="google_calendar",
    display_name="Google Calendar",
    version="1.0.0",
    type="bidirectional",  # read/write scope + create_event tool
    description="Multi-account Google Calendar sync (read) plus event creation (write).",
    icon="Calendar",
    models=["CalendarEvent"],
    embedding_sources=[],
    reads_from=["google-calendar-api"],
    writes_to=["google-calendar-api"],
    schedule="*/15 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=2,  # services/freshness.py: 120s
    staleness_probe=None,  # not in data_freshness.py's DATA_FRESHNESS_THRESHOLDS
    background_tasks=[],
    routes=[],
    # google_client_id/secret are the shared OAuth app registration (kernel
    # setting — used by every Google-scoped integration, not this one's own).
    config_schema={
        "calendar_visibility": ConfigFieldSpec(
            type="dict_str_str",
            default={
                "user@gmail.com": "full",
                "user@work.com": "busy",
                "family@gmail.com": "hidden",
            },
            description='Per-account visibility: "full" | "busy" | "hidden".',
        ),
    },
    oauth=OAuthRequirement(
        provider="google",
        scopes=["https://www.googleapis.com/auth/calendar"],
    ),
    provides=["calendar.query"],  # facade: app.integrations.google_calendar.facade — consumed by system
    depends_on=[],
)
