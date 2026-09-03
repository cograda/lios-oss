"""Manifest for the Strava integration."""

from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest

MANIFEST = IntegrationManifest(
    name="strava",
    display_name="Strava",
    version="1.0.0",
    type="source",
    description=(
        "Strava activity history — runs, rides, walks and gym sessions with "
        "distance, pace, elevation, heart rate and power."
    ),
    icon="Bike",
    models=["StravaActivity"],
    embedding_sources=[],
    reads_from=["strava-api"],
    writes_to=[],
    # Every 30 minutes. Strava activities appear minutes-to-hours after the
    # ride finishes (watch sync, then Strava's own processing), so a tighter
    # schedule buys nothing but API quota. Each run is one request when
    # there is nothing new.
    schedule="*/30 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    # Deliberately no staleness probe, and this is the considered choice
    # rather than an omission.
    #
    # ⚠️ A probe on `start_date` asks "has this person exercised recently",
    # which is not a fault condition — a fortnight off is a holiday, and an
    # alert that fires on one trains you to ignore the alert. A probe on
    # `synced_at` stays green for as long as the *scheduler* runs, which
    # `SyncState` already reports, so it would detect nothing new.
    #
    # This repo has twice thresholded a source without measuring its cadence
    # first (lastfm, 13 and 19 August — both firings were comar working
    # correctly). There is no Strava cadence history here yet to derive a
    # threshold from. `strava_status` reports coverage on demand; revisit
    # this once there are a few months of real data to measure.
    staleness_probe=None,
    background_tasks=[],
    # Strava's OAuth is its own flow — see routes.py's module docstring for
    # why it cannot ride app/auth/oauth.py.
    routes=["app.integrations.strava.routes:router"],
    config_schema={
        # Both deliberately NOT required. `required=True` would flip
        # `is_configured()` to False, which gates the ENTIRE integration off
        # — including the read-only tools over activities already in
        # Postgres, which need no credentials at all. Instead
        # `sync._credentials()` raises a PermanentError naming exactly which
        # key is missing, at the one call site that needs them. This is the
        # pattern server/CLAUDE.md prescribes under "Platform vs
        # personalisation", rule 2.
        "strava_client_id": ConfigFieldSpec(
            type="str",
            required=False,
            secret=False,
            description=(
                "Client ID of the Strava API application "
                "(https://www.strava.com/settings/api)."
            ),
        ),
        "strava_client_secret": ConfigFieldSpec(
            type="str",
            required=False,
            secret=True,
            description="Client secret of the Strava API application.",
        ),
    },
    # `oauth` is the kernel's GOOGLE scope union (app/auth/oauth.py builds one
    # consent screen from every manifest's scopes). Strava is a different
    # provider entirely, so this stays None and the flow lives in routes.py.
    oauth=None,
    # No `provides`: nothing else needs to call into Strava yet, and
    # writing-an-integration.md is explicit that a facade is not added
    # speculatively. Add one when `system`'s briefing actually wants it.
    provides=[],
    depends_on=[],
)
