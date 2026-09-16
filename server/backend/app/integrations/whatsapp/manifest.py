from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, StalenessProbe, TaskSpec

MANIFEST = IntegrationManifest(
    name="whatsapp",
    display_name="WhatsApp",
    version="1.0.0",
    type="push_source",  # bridge writes directly to Postgres, no pull sync
    description="WhatsApp messages/contacts captured by the Baileys bridge sidecar; conversation-window embedding.",
    icon="MessageCircle",
    models=["WhatsAppMessage", "WhatsAppContact"],
    embedding_sources=["whatsapp"],
    reads_from=["whatsapp-bridge"],
    writes_to=[],  # sendMessage/sendReadReceipt/presence all stubbed — read-only today
    schedule="*/30 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=1,  # services/freshness.py: 60s
    staleness_probe=StalenessProbe(
        model="WhatsAppMessage",
        timestamp_column="timestamp",
        threshold_minutes=24 * 60,
    ),
    background_tasks=[
        TaskSpec(
            name="whatsapp_bridge_heartbeat",
            target="app.integrations.whatsapp.heartbeat:run_heartbeat",
            kind="cron",
            cron="* * * * *",
            misfire_grace_time=30,  # matches pre-3.1 scheduler.py (every-minute job)
            # Wave 5.11: this probe already writes its own `SyncState` row
            # every run (`heartbeat._probe_one` -> `_update_sync_state`), and
            # `system_alerts`' axis 1 (consecutive_failures/staleness on that
            # row) is what detects the bridge going silent — independent of
            # whether this job is also `runs`-ledgered. A `runs` row here
            # would be 1,440 identical `ok` entries a day for zero extra
            # information, so it's excluded from the ledger entirely.
            ledger=False,
        ),
    ],
    routes=[],
    config_schema={
        "whatsapp_bridge_url": ConfigFieldSpec(
            type="str", required=False,
            description="Baileys bridge base URL (empty = integration's own docker-network default).",
        ),
        "whatsapp_self_chat_jids": ConfigFieldSpec(
            type="dict_str_str", required=False,
            description=(
                "Message-to-self chats (WhatsApp's 'Message yourself'), as "
                "{user_id: jid} — e.g. {\"1\": \"1234567@lid\"}. These are "
                "note-taking surfaces, not conversations, so each message is "
                "embedded as its own chunk instead of being grouped into a "
                "30-minute window with unrelated notes, and each is routed into "
                "that user's inbox for triage. Find a JID with "
                "`whatsapp_contacts` — the entry bearing your own name. "
                "Empty means no chat gets the treatment.\n\n"
                "⚠️ MUST be keyed by user. A WhatsApp `@lid` is scoped to the "
                "account that observed it, NOT global: the same LID string "
                "resolves to different conversations under different bridges. "
                "This was a flat list for one afternoon and the consequence was "
                "immediate — Alex's self-chat LID matched 178 rows under Sam's "
                "bridge, which were messages *received from a third party*, and "
                "59 of them were filed into her inbox as her own notes. Nothing "
                "crossed users (rows carry their owner), but the meaning was "
                "wrong, and 'user-scoped rows' is not the same guarantee as "
                "'user-scoped identifiers'."
            ),
        ),
        "whatsapp_bridge_urls": ConfigFieldSpec(
            type="list_str", required=False,
            description=(
                "Additional Baileys bridges beyond the primary, one per extra "
                "user — a Baileys session is bound to one phone number, so a "
                "second person needs a second container. Each is heartbeated "
                "separately (`whatsapp_bridge_2`, `_3`, …) so one dead bridge "
                "can't be masked by another answering."
            ),
        ),
    },
    oauth=None,
    provides=["whatsapp.query"],  # facade: app.integrations.whatsapp.facade — consumed by system
    depends_on=[],
)
