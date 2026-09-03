from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, TaskSpec

MANIFEST = IntegrationManifest(
    name="system",
    display_name="System",
    version="1.0.0",
    type="capability",  # V4 chunk 4.2: reclassified from "system" — no external system, in-process only
    description="Cross-integration diagnostics: health alerts, morning briefing, week ahead, search everything.",
    icon="Activity",
    models=[],  # no models.py — queries other integrations' tables directly
    embedding_sources=[],
    reads_from=[],
    writes_to=[],
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[
        TaskSpec(
            name="daily_brief_prewarm",
            target="app.integrations.system.brief:run_prewarm",
            kind="cron",
            # Every 15 minutes through the morning, not once at a fixed hour.
            # A single 06:00 warm goes stale for anyone who opens their note
            # at 09:30, and household members don't start their days together.
            # Outside this window the live path just fetches normally.
            cron="*/15 5-11 * * *",
            misfire_grace_time=300,
        ),
    ],
    routes=[],
    config_schema={
        "daemon_silent_minutes": ConfigFieldSpec(
            type="int",
            required=False,
            default=540,
            description=(
                "Minutes a daemon token (system_alerts axis 5) may go quiet "
                "before 'daemon silent' fires. Default is deliberately "
                "sleep-tolerant (was a hardcoded 20) — a laptop with the lid "
                "closed for the night is indistinguishable from a dead "
                "daemon to a flat elapsed-time check, and 20 minutes made "
                "`macbook:daemon_silent` flap every 30-60 minutes around the "
                "clock. See `notifications`' persistence gate / re-fire "
                "cooldown / quiet hours for the delivery-side half of this "
                "fix — this key only controls detection."
            ),
        ),
    },
    oauth=None,
    # `system.alerts` — added 2026-07-31 for `notifications`, which sweeps this
    # payload on a cron and pushes the delta. The facade method it resolves to
    # (`SystemFacade.alerts`) already existed for the dashboard route; this only
    # declares it as a real capability so a second consumer can depend on it
    # without importing this package's internals.
    provides=["system.alerts"],
    depends_on=[
        "health.query",
        "health.coverage",
        "reminders.query",
        "commute.query",
        "calendar.query",
        "mail.query",
        "homeassistant.entities",
        "vault.query",
        "weather.query",
        "whatsapp.query",
        # Added for `system_daily_brief`, which composes every read-only
        # source the /daily-note command used to fetch as 23 separate MCP
        # round-trips. Each of these five had a facade but no declared
        # capability, because until now their only callers were kernel
        # routes with fixed 1:1 dependencies.
        "rail.query",
        "attachments.query",
        "music.query",
        "coffee.query",
    ],
    # Deliberately NOT depending on `inbox.query`, though the daily note does
    # surface pending inbox files. `inbox` depends on `notify.push`, and
    # `notifications` depends on `system.alerts` — so consuming inbox here
    # closes the cycle inbox -> notifications -> system -> inbox, which
    # `app.plugin.validate._check_dependency_graph` rejects at boot.
    #
    # That rejection is correct rather than inconvenient: `system` both
    # provides alerts and consumes nearly everything, so it can't also sit
    # downstream of anything that needs alerting. The structural fix is to
    # split the alerts computation into its own provider; until then the
    # daily-note command calls `inbox_pending` itself, which is a fair
    # description of what it is anyway — an interactive triage step, not part
    # of the read-only morning snapshot.
)
