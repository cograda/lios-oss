"""Manifest for `transcription` — audio to text, as a capability.

Owns exactly one thing: "given an audio file on disk, return its text". It has
no queue, no schedule and no tables — whoever holds the audio drives it. Today
that's `inbox`, whose cron walks its own pending buckets and calls in here via
`transcription.audio`.

Why a separate package rather than a few functions inside `inbox`: the ability
to transcribe is not inbox-specific. WhatsApp voice notes and the `media` store
are the obvious next callers, and neither should have to import inbox internals
to get at it. A capability with one provider and one consumer today is the
cheapest way to keep that door open — and per V4's north star, adding it costs
zero kernel edits.

Deliberately NOT here:
  - No `models`. Transcripts are written into the caller's own record (for
    inbox, its `.meta.json` sidecar). A `transcriptions` table would duplicate
    state that already has an owner, and force a second dedup question.
  - No `schedule`/`background_tasks`. This package never decides *when* to
    transcribe; that's the caller's business and the caller's cost.
  - No MCP tools, same reasoning as `sheets` — a pure facade for other
    integrations, so there is nothing here a model should call directly.

Config is not `required`: `is_configured()` gating would silently disable the
capability, and the consumer would then have to distinguish "not installed"
from "installed but keyless". Instead `facade.available()` answers that
explicitly and callers skip cleanly. See `gemini.py`.

**Gemini-only as of W2 chunk 2** (owner decision, 2026-08-26 — see the plan
doc's "Decided: transcription is Gemini-only"). `openai_api_key`, the
`provider` selector, the OpenAI `model` field and `max_file_mb` are gone —
grepped repo-wide before removal and nothing outside `transcription/` and
its own tests read `openai_api_key`, so nothing else needed it kept. The
paid model is now `gemini_model`, resolved through the role registry
(`app.services.ai_roles`, role `stt.memo`) rather than read here directly.
"""

from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest

MANIFEST = IntegrationManifest(
    name="transcription",
    display_name="Transcription",
    version="1.0.0",
    type="capability",
    description="Transcribes audio to text via Gemini, with a proper-noun prompt built from the vault.",
    icon="AudioLines",
    models=[],
    embedding_sources=[],
    reads_from=["gemini-api"],
    writes_to=[],
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={
        "gemini_api_key": ConfigFieldSpec(
            type="str",
            required=False,
            secret=True,
            description=(
                "Google AI Studio API key for Gemini transcription — the same "
                "credential shape `vision` and `embedding` use, NOT a GCP "
                "service account. (Cloud Speech-to-Text/Chirp needs a service "
                "account and GCS-backed batch jobs for diarization, so it is "
                "deliberately not the provider here.)"
            ),
        ),
        "gemini_model": ConfigFieldSpec(
            type="str",
            required=False,
            default="gemini-3.7-flash",
            description=(
                "Gemini model for transcription. Flash rather than the "
                "Flash-Lite `vision` uses: measured head-to-head on a real "
                "memo (2026-08-07), Flash-Lite mis-heard words Flash resolved "
                "correctly and fell back to [inaudible] on a passage Flash "
                "transcribed cleanly. Audio bills at 32 tokens/second "
                "(1 min ~= 1,920 tokens), so even with thinking tokens a memo "
                "costs a fraction of a cent — not worth trading accuracy for. "
                "Diarization is prompted, not a model feature, so it survives "
                "a model swap. Bumped 2026-08-14 to `gemini-3.7-flash` after a "
                "real 7m04s two-speaker recording benched it against "
                "3.6-flash: half the cost, half the latency, no quality loss. "
                "Do NOT drop to `gemini-3.5-flash-lite` — the same bench found "
                "it merges two distinct speakers into one unlabelled block, a "
                "diarization failure severe enough that character-agreement "
                "scoring (0.948) could not even detect it."
            ),
        ),
        "gemini_max_file_mb": ConfigFieldSpec(
            type="int",
            required=False,
            default=18,
            description=(
                "Routing threshold for Gemini: files up to this size ride inline "
                "in the request (below Gemini's 20MB whole-request ceiling, which "
                "the prompt shares); larger files are uploaded via the Files API "
                "first and referenced by URI. Not a refusal since 2026-09-02."
            ),
        ),
        "dictionary_from_vault": ConfigFieldSpec(
            type="bool",
            required=False,
            default=True,
            description=(
                "Build the proper-noun prompt from People notes' `aliases` "
                "frontmatter in the caller's vault. Those aliases already "
                "record known voice-memo mis-transcriptions, so this dictionary "
                "maintains itself as a side effect of normal vault upkeep."
            ),
        ),
        "extra_dictionary_terms": ConfigFieldSpec(
            type="list_str",
            required=False,
            default=[],
            description=(
                "Additional proper nouns to hint, for things with no People "
                "note — place names, product names, jargon."
            ),
        ),
    },
    oauth=None,
    provides=["transcription.audio"],
    depends_on=[],
)
