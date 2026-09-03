from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, StalenessProbe

MANIFEST = IntegrationManifest(
    name="apple_health",
    display_name="Apple Health",
    version="1.0.0",
    type="push_source",  # data only ever arrives via a push from the iOS app
    description="Daily health metrics, workouts, and sleep pushed from the Health Auto Export iOS app.",
    icon="Heart",
    models=["HealthDailyMetric", "HealthWorkout", "HealthSleepSession"],
    embedding_sources=[],
    reads_from=[],
    writes_to=[],
    schedule=None,  # push-based, no server-side schedule
    schedule_timezone=None,
    freshness_threshold_minutes=None,  # not in services/freshness.py FRESHNESS_THRESHOLDS
    staleness_probe=StalenessProbe(
        model="HealthDailyMetric",
        timestamp_column="synced_at",
        # 36h is deliberately generous and measured, not lazy: real export gaps
        # run 12-20h (2026-08-16 21:34 -> 08-17 12:51 is 15h17m), so a tighter
        # threshold would alarm every night. What was actually broken is that the
        # figure was table-wide — it read 5h (Sam's phone) while Alex's was 9h.
        threshold_minutes=36 * 60,
        per_user=True,
    ),
    background_tasks=[],
    routes=["app.integrations.apple_health.routes:router"],
    # ui_token stays a kernel setting (shared UI-auth secret) — apple_health
    # merely accepts it as an alternate bearer on its push route, it isn't
    # this integration's own credential.
    config_schema={
        "health_push_token": ConfigFieldSpec(
            type="str", required=True, secret=True,
            description="Bearer token gating the Health Auto Export push endpoint.",
        ),
    },
    oauth=None,
    # facade: app.integrations.apple_health.facade — both consumed by `system`
    # (`health.query` by the morning briefing, `health.coverage` by the alerts
    # axis that catches date holes the staleness probe cannot see).
    provides=["health.query", "health.coverage"],
    depends_on=[],
)
