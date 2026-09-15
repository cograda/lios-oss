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
        # Added 2026-09-07: also hourly across the rest of the day. `/kickoff`
        # and `/checkin` (`/refresh` until 2026-09-10) are used well past the morning window above (an
        # evening catch-up, a second look at lunchtime), and `render=True`'s
        # cacheable rendered fragments (Coffee/Snags/Listening — see
        # `brief_render.NEVER_CACHE_RENDERED`) go stale the same way the raw
        # cacheable sources always have outside 05-11. Two `TaskSpec`s
        # (`TaskSpec.cron` takes one crontab string, and `*/15 5-11,*
        # 0-4,12-23 * * *`-style unions aren't how cron ranges compose) rather
        # than one — same `target`, same `name` prefix so both show up
        # together in `system_runs`/`recent_runs`.
        TaskSpec(
            name="daily_brief_prewarm_hourly",
            target="app.integrations.system.brief:run_prewarm",
            kind="cron",
            cron="0 * * * *",
            misfire_grace_time=300,
        ),
    ],
    routes=[],
    config_schema={
        # conversations_since (lios#192) — the burst-gap that splits one
        # WhatsApp chat's messages into separate conversations. See
        # `conversations.py` module docstring for why this is a config key
        # rather than a constant: the design doc (Daily Kickoff — Phase
        # contract, 2026-09-10 §4) treats the gap as a tunable, not a fact
        # about WhatsApp.
        "conversations_burst_gap_minutes": ConfigFieldSpec(
            type="int",
            required=False,
            default=360,
            description=(
                "Minutes of silence in one WhatsApp chat before "
                "conversations_since starts a new conversation group "
                "within that chat. Gmail groups by its own thread_id "
                "instead and ignores this. Default 6 hours."
            ),
        ),
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
        # index_state rebuild threshold (Wave 5.2) — see
        # `_index_state`/`_index_rebuild_pending_threshold` in tools.py.
        "index_rebuild_pending_threshold": ConfigFieldSpec(
            type="int",
            required=False,
            default=50,
            description=(
                "queue_pending above this many rows means a rebuild is "
                "actually happening, not just the steady-state lag of the "
                "5-minute embedding worker (measured ~3 rows in production). "
                "`rebuilding` is also true regardless of this threshold "
                "while an `embedding_reembed` run is in progress (see "
                "`app/scripts/reembed.py`'s `fill-space`, which bypasses the "
                "queue entirely and would otherwise report `rebuilding: "
                "false` for its whole run)."
            ),
        ),
        # Restore drill (R2, 2026-09) — see app/integrations/system/restore_drill.py.
        "restore_drill_stage_dir": ConfigFieldSpec(
            type="str",
            required=False,
            default="/data/backups",
            description=(
                "Directory the restore drill scans for `comar-db-*.dump` — "
                "bind-mounted read-only into the app container from the "
                "same host staging dir `lios-db-backup.sh` writes to "
                "(`STAGE` there, `HOME_BACKUPS_HOST_PATH` here). See "
                "docker-compose.yml."
            ),
        ),
        "restore_drill_remote": ConfigFieldSpec(
            type="str",
            required=False,
            default="",
            description=(
                "rclone remote:path to pull the latest dump from if the "
                "staging copy is gone (matches `lios-db-backup.sh`'s "
                "REMOTE, e.g. `gdrive-personal:comar-db-backups`). Empty "
                "disables the fallback — a missing staging copy then just "
                "fails the drill, loudly, rather than silently skipping."
            ),
        ),
        "restore_drill_rclone_bin": ConfigFieldSpec(
            type="str",
            required=False,
            default="rclone",
            description="Path to the rclone binary, if the remote fallback is used.",
        ),
        "restore_drill_stale_days": ConfigFieldSpec(
            type="int",
            required=False,
            default=8,
            description=(
                "Days since the last SUCCESSFUL restore drill before "
                "`days_since_restore_drill` degrades system_alerts. The "
                "drill itself runs weekly, so 8 tolerates one run landing a "
                "day late without alarming."
            ),
        ),
        "restore_drill_top_n_tables": ConfigFieldSpec(
            type="int",
            required=False,
            default=10,
            description=(
                "How many of the live database's largest tables (by "
                "pg_stat_user_tables row estimate) the drill row-count-checks "
                "against the restored scratch database."
            ),
        ),
        "restore_drill_volatile_tables": ConfigFieldSpec(
            type="list_str",
            required=False,
            default=["embedding_queue"],
            description=(
                "Tables the drill never row-count-checks because their count "
                "is not a restore-fidelity signal: work queues and outboxes "
                "that legitimately grow or drain by an order of magnitude "
                "between the nightly dump and the drill. Found on the first "
                "live run (2026-09-04): embedding_queue was 1,105 rows at dump "
                "time and 8,871 fourteen hours later — ratio 0.12 against a "
                "0.9 floor — while every real table matched to 0.1%."
            ),
        ),
        "restore_drill_notify_targets": ConfigFieldSpec(
            type="list_str",
            required=False,
            default=[],
            description=(
                "HA `notify.<target>` service names (no `notify.` prefix) "
                "pushed to when the drill itself fails — the same "
                "mechanism `lios-db-backup.sh`'s notify_failure uses, "
                "reached here via the `homeassistant.notify` capability. "
                "Empty means a failed drill is visible only in "
                "system_alerts / the dashboard, not pushed."
            ),
        ),
        # Strand A — dead-board alerting (system_alerts axis 11). See
        # app/integrations/system/device_fleet.py for the full design and
        # `contracts/device-contract.md` §4 for the availability semantics
        # this reads.
        "device_fleet_registry_path": ConfigFieldSpec(
            type="str",
            required=False,
            default="/contracts/fleet.md",
            description=(
                "Path (inside the container) to the canonical fleet "
                "registry, `contracts/fleet.md` — bind-mounted read-only "
                "from the repo root via `HOME_CONTRACTS_HOST_PATH` (see "
                "docker-compose.yml), same pattern as the vault/doc_corpus/"
                "backups mounts. An unset host path mounts `/dev/null` "
                "there, so this resolves to nothing until configured — a "
                "missing/empty registry is a HARD REFUSAL (see "
                "device_fleet.FleetRegistryError), never a silent 'no "
                "boards, nothing wrong'. Falls back to the checked-out "
                "repo's own contracts/fleet.md (five parents up from this "
                "file) when the configured path doesn't exist — the path "
                "`make dev`/pytest actually run against, since neither "
                "goes through this bind mount."
            ),
        ),
        "device_fleet_stale_minutes": ConfigFieldSpec(
            type="int",
            required=False,
            default=30,
            description=(
                "Minutes a board's diagnostic entities (sensor.<prefix>_"
                "uptime etc, per contracts/device-contract.md §4) may go "
                "without a state change before the board is flagged dead. "
                "Per-board override: device_fleet_stale_minutes_overrides."
            ),
        ),
        "device_fleet_stale_minutes_overrides": ConfigFieldSpec(
            type="dict_str_str",
            required=False,
            default={},
            description=(
                "Per-board override of device_fleet_stale_minutes, keyed "
                "by the board's fleet.md node name (e.g. "
                "{\"rack-monitor\": \"90\"}) — string minutes, parsed at "
                "read time. For a battery/solar or deliberately-"
                "intermittent board that would false-alarm on the "
                "household default (Strand A4)."
            ),
        ),
        # Host-liveness alerting (system_alerts axis 12). See
        # app/integrations/system/host_fleet.py for the full design and
        # contracts/hosts.md for the registry it reads — companion to the
        # device_fleet_* keys above, one layer down (hosts, not boards).
        "host_fleet_registry_path": ConfigFieldSpec(
            type="str",
            required=False,
            default="/contracts/hosts.md",
            description=(
                "Path (inside the container) to the canonical host "
                "registry, contracts/hosts.md — bind-mounted read-only "
                "from the repo root via HOME_CONTRACTS_HOST_PATH, the "
                "same mount device_fleet_registry_path already uses (it "
                "covers the whole contracts/ directory). An unset host "
                "path mounts /dev/null there, so this resolves to nothing "
                "until configured — a missing/empty registry is a HARD "
                "REFUSAL (see host_fleet.HostRegistryError), never a "
                "silent 'no hosts, nothing wrong'. Falls back to the "
                "checked-out repo's own contracts/hosts.md (six parents "
                "up from host_fleet.py) when the configured path doesn't "
                "exist — the path make dev/pytest actually run against."
            ),
        ),
        "host_fleet_stale_minutes": ConfigFieldSpec(
            type="int",
            required=False,
            default=60,
            description=(
                "Minutes an `ha_entity:<id>` host probe's HA `last_updated` "
                "may go without a fresh write before the host is flagged "
                "dead (issue #167: sensor.shed_cam_temperature sat frozen "
                "for nine days and read as 'ok' throughout, because a "
                "number is not 'unavailable' — a frozen sensor is "
                "indistinguishable from a stable one by value alone). No "
                "per-host override, unlike device_fleet_stale_minutes_"
                "overrides — today's hosts.md has exactly one ha_entity-"
                "probed host; add one if a second needs a different "
                "tolerance."
            ),
        ),
        "pulse_url": ConfigFieldSpec(
            type="str",
            required=False,
            default="",
            description=(
                "Base URL of the Pulse monitoring instance (e.g. "
                "http://192.168.1.2:7655), same convention as "
                "~/.airq/pulse.env's PULSE_URL. Empty means every "
                "pulse-probed host in contracts/hosts.md reports "
                "'unknown' with reason 'probe_unconfigured' — never "
                "'ok' (host_fleet.py's hard rule 2)."
            ),
        ),
        "pulse_token": ConfigFieldSpec(
            type="str",
            required=False,
            secret=True,
            default="",
            description=(
                "Pulse API token (X-API-Token header), same convention "
                "as ~/.airq/pulse.env's PULSE_TOKEN. Empty has the same "
                "effect as an empty pulse_url — see that key."
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
        # Restore drill (restore_drill.py) pushes its own failure through
        # this — the same HA notify.<target> mechanism lios-db-backup.sh's
        # notify_failure uses, reached via the existing capability instead
        # of a second env-file-reading implementation.
        "homeassistant.notify",
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
        # `system_alert_log` (lios#230) + the daily brief's "Monitoring
        # since last note" sub-block — the reviewable log of Alertmanager
        # webhook deliveries (`app/integrations/alerts/`).
        "alerts.query",
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
