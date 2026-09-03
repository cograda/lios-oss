from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, StalenessProbe, TaskSpec

MANIFEST = IntegrationManifest(
    name="apple_reminders",
    display_name="Apple Reminders",
    version="1.0.0",
    type="bidirectional",  # server enqueues -> SSE -> client daemon executes EventKit
    description="Two-way sync with Apple Reminders via EventKit through the client daemon.",
    icon="CheckSquare",
    models=["Reminder", "ReminderCommand"],
    embedding_sources=[],
    reads_from=["eventkit"],
    writes_to=["eventkit"],
    schedule=None,  # no server-side schedule — Mac agent pushes on its own timer
    schedule_timezone=None,
    freshness_threshold_minutes=2,  # services/freshness.py: 120s
    staleness_probe=StalenessProbe(
        # Bridge liveness, not data-change time — see data_freshness.py::_probe.
        # Keyed off the core Users table, not this package's own models.
        model="User",
        timestamp_column="reminders_verified_at",
        # 2026-08-27: raised from 5 to 540 (9h), and made config-overridable
        # (`reminders_stale_minutes` below) — a MacBook with the lid closed
        # overnight looks identical to a dead daemon at 5 minutes, and
        # `apple_reminders:data_stale` flapped every 30-60 minutes around the
        # clock (vault/Projects/lios/Backlog.md, "Push notifications flap
        # all night"). See `notifications/sweep.py`'s push-boundary gating
        # for the delivery-side half of that fix — this threshold only
        # controls when the underlying condition is considered "stale" at
        # all. See tests/test_data_freshness.py::TestManifestDrivenProbeSet
        # .DELIBERATE_CHANGES for this being a recorded, deliberate change
        # rather than silent drift.
        threshold_minutes=540,
        threshold_config_key="reminders_stale_minutes",
        # Per owner: a table-wide MAX() reports the freshest daemon across the
        # household, so one live Mac hides another that has stopped. Measured
        # 2026-08-17 — this read 0m (Alex) while Sam's had been silent 46
        # minutes, and nothing alerted. `user_column` is `id` because this probes
        # the core Users table, not a UserOwnedMixin table.
        per_user=True,
        user_column="id",
    ),
    background_tasks=[
        TaskSpec(
            name="backlog_sync",
            target="app.integrations.apple_reminders.backlog_sync:run_scheduled_sync",
            kind="cron",
            cron="*/30 * * * *",
        ),
    ],
    routes=["app.integrations.apple_reminders.routes:router"],
    # ui_token and obsidian_vault_path are kernel settings this integration
    # reads directly (UI auth secret + the vault mount path) — not its own
    # config.
    config_schema={
        # Declared but not yet read by backlog_sync.py (which does local
        # difflib fuzzy matching today, not a Haiku call) — kept so the key
        # exists ahead of that work rather than living as a dead HomeSettings
        # field. See server/CLAUDE.md's Known Issues note on backlog sync.
        # ⚠️ Default OFF, and that is load-bearing. Until 2026-08-19 this
        # direction wrote `ReminderCommand` rows that nothing dispatched and
        # nothing drained — 107,058 pending rows over five months. Defaulting it
        # on would convert a five-month silent no-op into one real reminder per
        # unmatched backlog task on the next 30-minute tick. Turn it on
        # deliberately and watch the first run.
        "reminders_push_vault_to_reminders": ConfigFieldSpec(
            type="bool",
            required=False,
            default=False,
            description=(
                "Whether backlog_sync pushes unmatched open vault tasks into "
                "Apple Reminders. Off means the sync is Reminders→vault only, "
                "and the count of un-pushed tasks is reported as "
                "`vault_only_not_pushed` so the gap is visible rather than "
                "silent."
            ),
        ),
        "anthropic_api_key": ConfigFieldSpec(
            type="str", required=False, secret=True,
            description="Anthropic API key (not yet consumed — reserved for Haiku-powered task matching).",
        ),
        "reminders_stale_minutes": ConfigFieldSpec(
            type="int",
            required=False,
            default=540,
            description=(
                "Minutes since a Mac's `reminders_verified_at` heartbeat "
                "before its daemon is reported stale (system_alerts axis 2, "
                "per-owner). Default is deliberately sleep-tolerant — see "
                "the `staleness_probe` comment in this manifest for why the "
                "old 5-minute value was a false-positive generator, not a "
                "genuinely tighter check."
            ),
        ),
    },
    oauth=None,
    provides=["reminders.query"],  # facade: app.integrations.apple_reminders.facade — consumed by system
    depends_on=[],
)
