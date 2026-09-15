from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, StalenessProbe

MANIFEST = IntegrationManifest(
    name="lastfm",
    display_name="Last.fm",
    version="1.0.0",
    type="source",
    description="Last.fm scrobble history sync with resumable, retrying backfill.",
    icon="Music",
    models=["Scrobble", "ArtistTag"],
    embedding_sources=[],
    reads_from=["lastfm-api"],
    writes_to=[],
    schedule="*/15 * * * *",
    schedule_timezone=None,
    freshness_threshold_minutes=15,  # services/freshness.py: 900s
    staleness_probe=StalenessProbe(
        model="Scrobble",
        timestamp_column="played_at",
        # 7 days, raised from 48h on 2026-08-19 after measuring the actual
        # cadence instead of guessing at it.
        #
        # Over the last 4,000 scrobbles (back to 2025-11-29) there are **48 gaps
        # of ≥24h, 4 of ≥48h, and a largest of exactly 72.0h** — so a 48-hour
        # threshold sits *inside* the normal distribution and fires roughly four
        # times a year on nothing but a quiet weekend. That is why this alert
        # recurred on 13 and 19 August and was investigated as a fault twice;
        # both times comar was correctly in sync.
        #
        # ⚠️ `played_at` is when the track was *played*, not when it was
        # ingested, and the Scrobbler app submits in **batches with lag** — on
        # 19 Aug it flushed two days of plays in a single burst, during which
        # Last.fm's own API had genuinely never heard of them. So the threshold
        # has to cover the largest listening gap (72h) *plus* submission lag.
        #
        # What this probe uniquely detects is a permanently dead scrobbler; a
        # broken sync is already covered by SyncState. A week catches the former
        # without ever firing on the latter.
        threshold_minutes=7 * 24 * 60,
    ),
    background_tasks=[],
    routes=[],
    config_schema={
        "lastfm_api_key": ConfigFieldSpec(type="str", required=True, secret=True, description="Last.fm API key."),
        "lastfm_usernames": ConfigFieldSpec(
            type="dict_str_str",
            required=False,
            description=(
                "Map of comar user name to Last.fm username, e.g. "
                '{"<comar-user>": "<lastfm-username>"}. Each entry syncs into '
                "that user's own scrobbles, so two people on one deployment "
                "keep separate listening histories."
            ),
        ),
        # Deliberately NOT required: `is_configured()` accepts either this or
        # `lastfm_usernames`, and marking it required would gate every tool off
        # for a deployment that has correctly moved to the map form.
        "lastfm_username": ConfigFieldSpec(
            type="str",
            required=False,
            description=(
                "DEPRECATED single-user form, kept so an existing deployment "
                "keeps syncing across the upgrade. Attributed to the "
                "lowest-id active user. Prefer `lastfm_usernames`."
            ),
        ),
    },
    oauth=None,
    provides=["music.query"],  # facade: app.integrations.lastfm.facade — consumed by system
    depends_on=[],
)
