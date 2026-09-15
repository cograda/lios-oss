from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, TaskSpec

MANIFEST = IntegrationManifest(
    name="signals",
    display_name="Signals",
    version="1.0.0",
    # "capability", not "source" — like `tasks`: there's no periodic
    # pull/sync to run (Protect pushes to the inlet route; the watcher tick
    # is a `background_tasks` cron entry, same shape as tasks_routines_tick).
    type="capability",
    description=(
        "Generic camera/sensor event inlet (UniFi Protect webhooks today) "
        "plus a watcher framework that grabs frames, asks vision a question, "
        "and pushes a household notification. First tenant: the milk watch."
    ),
    icon="Video",
    models=["SignalEvent", "WatchRun"],
    embedding_sources=[],
    # UniFi Protect posts here directly; the milk watcher pulls a frame from
    # the front-door RTSP stream. Both external, in the "reads_from" sense.
    reads_from=["unifi-protect-webhook", "rtsp-camera"],
    writes_to=[],
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[
        # Drives every registered watcher: opens a window when its schedule
        # starts, polls inside it, closes it (and pushes "no milk tonight"-
        # shaped notifications) when it ends. Every 5 minutes per the brief —
        # tight enough that a window closing without a detection is noticed
        # promptly, loose enough that it costs nothing between real events.
        TaskSpec(
            name="signals_watchers_tick",
            target="app.integrations.signals.runner:run_tick",
            kind="cron",
            cron="*/5 * * * *",
        ),
        # Frames are real photographs of the house exterior forever if
        # nothing prunes them. Daily, off-peak.
        TaskSpec(
            name="signals_prune_frames",
            target="app.integrations.signals.cameras:prune_frames_task",
            kind="cron",
            cron="17 3 * * *",
        ),
    ],
    routes=["app.integrations.signals.routes:router"],
    config_schema={
        # Env fallback for a schema key `signals_protect_key` is
        # `HOME_SIGNALS_PROTECT_KEY` — plugin_config()'s env fallback is
        # `HOME_{key.upper()}`, not integration-prefixed, so the key name
        # itself has to carry the "signals_" prefix to land on the env var
        # name the brief specifies.
        "signals_protect_key": ConfigFieldSpec(
            type="str",
            required=False,
            secret=True,
            description=(
                "Shared secret UniFi Protect's Alarm Manager webhook must "
                "present, as ?key= or X-Signal-Key. Without it the route "
                "accepts nothing (every request 401s) rather than accepting "
                "everything — see routes.py."
            ),
        ),
        "signals_devices": ConfigFieldSpec(
            type="dict_str_str",
            required=False,
            default={},
            description=(
                "Device key (MAC, lower-case) -> human-readable name, e.g. "
                '{"8c:ed:e1:72:f4:13": "front_door"}. An event from an '
                "unmapped device is still stored (device_name=null) and "
                "logged at INFO with its key, so the first real hit from a "
                "new device names itself."
            ),
        ),
        "signals_frame_retention_days": ConfigFieldSpec(
            type="int",
            required=False,
            default=30,
            description="Grabbed frames older than this are deleted by the daily prune job.",
        ),
    },
    oauth=None,
    # notify.push announces a milk detection or a closed empty window.
    # vision.image is declared as a dependency in spirit (this integration
    # calls vision's compare() directly via its facade, the same way any
    # capability consumer would) — see watchers/base.py.
    depends_on=["notify.push", "vision.image"],
    # No `provides` yet — nothing consumes signals data from another
    # integration today (deliberately not registering a dead capability;
    # see the `ai_roles.py` module docstring's rule on this). Add
    # "signals.query" the same change something first reads it.
    provides=[],
)
