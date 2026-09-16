from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, OAuthRequirement, StalenessProbe

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
    config_schema={
        "embed_backfill_from": ConfigFieldSpec(
            type="str",
            default="2026-07-15",
            description=(
                "ISO date floor for the gmail_embed backfill: only mail dated "
                "on or after this date is considered for embedding. Mail "
                "before it is covered by the historical timemachine import "
                "(embeddings.source='email', source_id like 'tm:%'), which "
                "predates this codebase (a bulk import, not written by "
                "anything under app/) and is intentionally never re-embedded "
                "here — its ~24k rows would otherwise all look 'unembedded' "
                "to a naive google_message_id anti-join and get billed again."
            ),
        ),
    },
    oauth=OAuthRequirement(
        provider="google",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    ),
    # facade: app.integrations.google_mail.facade — consumed by system
    # (unread + semantic search) and attachments (attachment fetch).
    provides=["mail.query"],
    depends_on=[],
)
