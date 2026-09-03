from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, TaskSpec

MANIFEST = IntegrationManifest(
    name="inbox",
    display_name="Inbox",
    version="1.0.0",
    type="source",
    description="Scans the vault Inbox landing zone and routes files into historical_corpus.",
    icon="Inbox",
    # F6 (2026-08-08): per-user ownership ledger — see models.py's docstring.
    models=["InboxItem"],
    embedding_sources=[],
    reads_from=[],
    writes_to=[],
    schedule="7 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[
        # Transcription is deliberately NOT part of the hourly `sync_inbox` job
        # or the inline ingest enrichment: it costs money per call and a long
        # memo takes minutes. Its own cron means a captured voice note is
        # readable within ~5 minutes instead of up to an hour, while each sweep
        # stays bounded (`transcribe_pending`'s `limit`) so a backfill walks
        # through over successive runs rather than issuing hundreds of paid
        # requests at once.
        TaskSpec(
            name="inbox_transcribe_pending",
            target="app.integrations.inbox.scan:transcribe_pending_task",
            kind="cron",
            cron="*/5 * * * *",
        ),
        # Images get their own sweep for the same reasons as audio (billable per
        # call, third-party dependency), on a slower cadence: a photographed
        # letter is not time-critical the way a voice note is, and images arrive
        # in bursts — a camera roll drop shouldn't race the transcriber for the
        # same five-minute window.
        TaskSpec(
            name="inbox_describe_pending",
            target="app.integrations.inbox.scan:describe_pending_task",
            kind="cron",
            cron="*/15 * * * *",
        ),
        # Pulling WhatsApp self-chat notes into the queue. Its own task rather
        # than part of the hourly `sync_inbox` because a note typed on a phone
        # should reach triage while the thought is still current — an hour is
        # long enough that the user goes looking for it by hand, which is the
        # behaviour this is meant to remove. Cheap enough for the cadence: one
        # indexed query plus, occasionally, a small file write. Nothing billable,
        # so unlike the transcribe/describe sweeps there is no spend to pace.
        TaskSpec(
            name="inbox_route_whatsapp_notes",
            target="app.integrations.inbox.whatsapp_notes:route_self_notes_task",
            kind="cron",
            cron="*/10 * * * *",
        ),
    ],
    routes=[],
    # inbox_path is a Docker volume mount path (infra concern) — stays a
    # kernel setting. inbox_token is this integration's own webhook secret.
    config_schema={
        "inbox_token": ConfigFieldSpec(
            type="str", required=False, secret=True,
            description="Bearer token for the Tines webhook ingestion route.",
        ),
        "inbox_whatsapp_note_max_age_days": ConfigFieldSpec(
            type="int", required=False, default=7,
            description=(
                "Only route WhatsApp self-chat notes captured within this many "
                "days. 0 means no limit.\n\n"
                "The triage queue is for things still worth acting on, and "
                "enabling this feature against an existing chat otherwise "
                "backfills its whole history in one sweep — the first live run "
                "put four months of notes (including pasted keys and hashes) into "
                "the queue at once. Note this bounds *routing* only, never "
                "embedding: search should cover every note ever written, so "
                "`whatsapp/sync.py` deliberately has no equivalent cutoff."
            ),
        ),
        "inbox_confirm_push": ConfigFieldSpec(
            type="bool", required=False, default=False,
            description=(
                "Push an arrival confirmation from the server on every ingest. "
                "Leave OFF while a relay (the Tines story) is still building that "
                "confirmation from the route's `summary` response, or every "
                "capture is announced twice. Turn ON when a producer posts here "
                "directly — an iOS Shortcut can send bytes but cannot announce "
                "what the server made of them, so without this a direct capture "
                "arrives silently."
            ),
        ),
    },
    oauth=None,
    # `transcription.audio` turns captured audio into the `note` field that
    # `summarise()` leads with; `notify.push` announces the finished transcript,
    # since the ingest-time confirmation necessarily predates it. `notify.email`
    # (added for the Tines retirement) reproduces the retired story's two
    # transcript emails — see `scan.py`'s `_notify_transcript_success_email`/
    # `_notify_transcript_failure_email`.
    #
    # `whatsapp.query` is the message-to-self capture channel, and the direction
    # is forced: `whatsapp` may NOT depend on `inbox`, because `system` (reachable
    # from here via notify.push → system.alerts) depends on `whatsapp.query`, and
    # boot validation rejects the resulting cycle. See
    # `inbox/whatsapp_notes.py` and `whatsapp/facade.py`.
    depends_on=[
        "corpus.ingest", "transcription.audio", "vision.image", "notify.push",
        "notify.email", "whatsapp.query",
    ],
)
