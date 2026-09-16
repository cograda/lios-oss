from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest

MANIFEST = IntegrationManifest(
    name="vision",
    display_name="Vision",
    version="1.0.0",
    type="capability",
    description=(
        "Describes and reads text from images — photographs of documents, "
        "screenshots, snag photos — for whoever is holding the file."
    ),
    icon="Eye",
    models=[],
    embedding_sources=[],
    reads_from=["gemini-api"],
    writes_to=[],
    # No schedule and no tools: whoever holds the image drives this, the same
    # way `inbox`'s cron drives `transcription`. A sweep here would need its own
    # queue, and the queue already exists next door.
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    # A quiet vision integration is the healthy state — most days bring no
    # images. Nothing to probe for staleness.
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={
        "gemini_api_key": ConfigFieldSpec(
            type="str",
            # Not required, deliberately. `required` gates the whole integration
            # off via `is_configured()`, and the house rule is to fail at the one
            # call site that needs the value instead — `facade.available()`
            # returns False so the inbox sweep no-ops quietly rather than
            # logging a failure for every pending image, every cron tick.
            required=False,
            secret=True,
            description=(
                "Google API key for Gemini vision. Without it the capability "
                "reports itself unavailable and images keep their existing "
                "dimensions-only preview."
            ),
        ),
        "model": ConfigFieldSpec(
            type="str",
            required=False,
            default="gemini-3.5-flash-lite",
            description=(
                "Vision model, chosen by measurement: 10 models x 8 real inbox "
                "images against a blind human reading (2026-08-05). Gemini "
                "because it bills images by a tile grid derived from aspect "
                "ratio rather than pixel count — a photo of a document costs "
                "~1,200 tokens whether it is 1MP or 24MP, where Anthropic "
                "charges (w*h)/750 and OpenAI per 32px patch — and because it "
                "is the only one of the three that accepts HEIC natively, "
                "which an inbox fed by iPhone photos cares about. "
                "Flash-Lite rather than Flash because quality barely tracked "
                "price: over six text-bearing images it scored 0.899 mean "
                "agreement against Flash's 0.855, at EUR 1.40/year versus "
                "EUR 24 (ten images a day, batch). The spread across all ten "
                "models was only 0.82-0.92, so this is a cheap seat in a "
                "crowded field rather than a compromise. Step up to "
                "'gemini-3.6-flash' or 'claude-sonnet-5' (0.923, the best "
                "measured) if a real workload disagrees. "
                "RE-TESTED 2026-08-14 when gemini-3.7-flash shipped, on four "
                "real images against this exact prompt: Flash-Lite held. All "
                "three models agreed on KIND for every image, and on the "
                "text-heavy screenshot the extracted text was character-"
                "identical except that Flash-Lite used the ' | ' table "
                "separator this prompt asks for and 3.7-flash dropped it — the "
                "dearer model followed the instruction worse. Cost per image: "
                "Flash-Lite $0.00093, 3.7-flash $0.00349, 3.6-flash $0.01002; "
                "latency 2.6s / 4.0s / 7.1s. The whole difference is reasoning "
                "tokens Flash-Lite does not spend (0 vs 186-1,308) and this "
                "job does not need. Two independent measurements now say the "
                "cheap model wins here."
            ),
        ),
        "watch_model": ConfigFieldSpec(
            type="str",
            required=False,
            default="gemini-3.5-flash-lite",
            description=(
                "Vision model for role 'vision.watch' (multi-image compare, "
                "used by the signals watcher framework — e.g. the milk "
                "watch). Separate field from 'model' (which serves "
                "vision.inbox) even though both currently default to the "
                "same measurement-backed choice — see "
                "app/services/ai_roles.py::resolve_vision_watch for why."
            ),
        ),
        "max_image_mb": ConfigFieldSpec(
            type="int",
            required=False,
            default=18,
            description=(
                "Skip images larger than this. Inline image bytes share a 20MB "
                "ceiling with the prompt, so this sits under it; the Files API "
                "is the escape hatch if genuinely huge scans ever turn up."
            ),
        ),
    },
    # Gemini is reached with an API key, not Google OAuth — this is not the
    # `oauth` path the calendar/mail integrations use.
    oauth=None,
    provides=["vision.image"],
    depends_on=[],
)
