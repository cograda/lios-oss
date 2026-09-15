from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, StalenessProbe

MANIFEST = IntegrationManifest(
    name="commute",
    display_name="Commute",
    version="1.0.0",
    type="source",
    description=(
        "Bus->rail commute solver over NTA GTFS-R + Irish Rail live data. "
        "Answers on demand at any hour via commute_query; the weekday-morning "
        "schedule below only logs decisions for history and buffer tuning."
    ),
    icon="Route",
    models=["CommuteDecision"],
    embedding_sources=[],
    reads_from=["nta-gtfs-api", "irish-rail-api"],
    writes_to=[],
    # Named days, NOT `1-5`. APScheduler's `CronTrigger.from_crontab` numbers
    # day-of-week 0=Monday..6=Sunday (like `date.weekday()`), not Vixie-cron's
    # 0=Sunday, despite advertising crontab syntax. `1-5` therefore meant
    # **Tue-Sat**, verified by enumerating fire times: this job silently skipped
    # every Monday morning — a commute day — and ran every Saturday, when nobody
    # commutes. Live since the integration was written; found 2026-08-13 while
    # making the staleness check schedule-aware. Named days are immune to the
    # quirk, so use them rather than swapping one magic number for another.
    schedule="0-57 7-8 * * mon-fri",
    schedule_timezone="Europe/Dublin",
    freshness_threshold_minutes=None,  # not in services/freshness.py — only the data_freshness probe below
    # 6 minutes was a false positive by construction: the job only runs
    # 07:00-08:57 on weekdays, so a correct, healthy commute integration looked
    # "stale" for ~22 hours of every day and all weekend — noise that trained us
    # to ignore system_alerts. The threshold has to span the longest expected
    # legitimate gap (Friday 08:57 -> Monday 07:00 is ~70h) for the probe to
    # mean anything. Real breakage surfaces as a sync error or a `degraded`
    # decision state, not as staleness — and the answer path users actually hit
    # (commute_query) is live, so it cannot go stale at all.
    staleness_probe=StalenessProbe(
        model="CommuteDecision",
        timestamp_column="decided_at",
        threshold_minutes=72 * 60,
    ),
    background_tasks=[],
    routes=[],
    # ha_token/ha_url are homeassistant's own config (this integration reads
    # them via plugin_config("homeassistant"), per depends_on below).
    config_schema={
        "nta_api_key": ConfigFieldSpec(
            type="str", required=True, secret=True,
            description="NTA GTFS-Realtime API key.",
        ),
        "commute_interchange_buffer_min": ConfigFieldSpec(
            type="int", default=6,
            description=(
                "Minutes of slack required at the bus/rail interchange. Tune "
                "from the logged interchange delay distribution."
            ),
        ),
        # --- Route definition (deployment config, 2026-07-28) --------------
        # These four used to be hardcoded Route constants in domain.py naming
        # this household's stops. The solver itself was always direction- and
        # route-agnostic; only the instances were personal.
        "commute_bus_home_stop": ConfigFieldSpec(
            type="str", default="",
            description="GTFS stop_id for the home-end bus stop (outbound boarding).",
        ),
        "commute_bus_home_return_stop": ConfigFieldSpec(
            type="str", default="",
            description=(
                "GTFS stop_id for the home-end stop in the return direction, "
                "if different (divided roads have two distinct stop ids). "
                "Defaults to commute_bus_home_stop."
            ),
        ),
        "commute_bus_interchange_stop": ConfigFieldSpec(
            type="str", default="",
            description="GTFS stop_id of the bus stop at the rail interchange.",
        ),
        "commute_rail_interchange_station": ConfigFieldSpec(
            type="str", default="",
            description="Rail station code at the interchange.",
        ),
        "commute_rail_city_station": ConfigFieldSpec(
            type="str", default="",
            description=(
                "Rail station code at the destination end. Note that Irish "
                "Rail's codes are not always the obvious spelling."
            ),
        ),
        "commute_bus_routes": ConfigFieldSpec(
            type="list_str", default=[],
            description=(
                "GTFS route_short_names to consider for the bus leg, e.g. "
                '["L1", "L2"]. Empty means any route serving both stops.'
            ),
        ),
        # Default flipped to true 2026-07-29: this was the first-week
        # buffer-tuning posture, and the tuning is done (73 clean interchange
        # samples, p50 1.6 / p90 6.3 min). Leaving it false was masking real
        # scheduled answers as "logging_only" indefinitely.
        "commute_surface_decisions": ConfigFieldSpec(
            type="bool", default=True,
            description=(
                "Surface scheduled decisions as real answers (and push them to "
                "Home Assistant). False masks them to a logging_only state — "
                "only useful while tuning commute_interchange_buffer_min on a "
                "fresh deployment."
            ),
        ),
        "commute_arrive_by": ConfigFieldSpec(
            type="str", default="08:57",
            description="HH:MM deadline the scheduled morning job solves for.",
        ),
    },
    oauth=None,
    provides=["commute.query"],  # facade: app.integrations.commute.facade — consumed by system
    depends_on=["homeassistant.entities"],
)
