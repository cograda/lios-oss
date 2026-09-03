from app.plugin.manifest import IntegrationManifest, OAuthRequirement, StalenessProbe

MANIFEST = IntegrationManifest(
    name="google_mail",
    display_name="Gmail",
    version="1.0.0",
    type="source",  # read-only today (dominant current behavior; see 4.x)
    description="Gmail OAuth sync with pgvector semantic search over message bodies.",
    icon="Mail",
    models=["MailMessage"],
    embedding_sources=["email"],
    reads_from=["gmail-api"],
    writes_to=[],
    schedule="*/15 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=2,  # services/freshness.py: 120s
    staleness_probe=StalenessProbe(
        model="MailMessage",
        timestamp_column="date",
        threshold_minutes=6 * 60,
    ),
    background_tasks=[],
    routes=[],
    # google_client_id/secret are the shared OAuth app registration (kernel
    # setting — used by every Google-scoped integration, not this one's own).
    config_schema={},
    oauth=OAuthRequirement(
        provider="google",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    ),
    # facade: app.integrations.google_mail.facade — consumed by system
    # (unread + semantic search) and attachments (attachment fetch).
    provides=["mail.query"],
    depends_on=[],
)
