"""Manifest for the `household` integration — Domains (Phase B) and the
capture inbox (Phase A2/A4) of
vault/Projects/lios/Plans/household-ops-and-loops-2026-08.md.

No external system, so no reads_from/writes_to, no schedule, and no
staleness probe — matches `snags`'s manifest shape (a `CapabilityService`
with `schedule=None`, since capture/writes are entirely user-gated tool
calls, never a scheduled sync).

`depends_on=["notify.push"]` (added with the capture inbox): the WhatsApp-
scan capture tool best-effort pushes a confirmation via
`get_capability("notify.push")` — see `tools.py::_notify_capture_confirmation`
for how that confirmation is routed to the capturer's own device via
`user_id`, never household-wide.

`config_schema.capture_keywords` (Tranche 2.5, 2026-08-28): which of
`capture.KINDS` the WhatsApp scanner actually watches for is now
deployment config, not a hardcoded tuple — "config controls what is
offered" (this file's own root CLAUDE.md rule). Default is `["task",
"discuss", "surface", "feedback"]`. **`nag` is deliberately excluded from
the default** — household-ops-and-loops-2026-08.md's open decision 6 flags
it as socially risky and explicitly undecided ("Are nag and surface both in
scope for A2?"). Making it a config key rather than a code change means
turning it on is a conscious household decision, not something that ships
silently the next time this package is touched. `household_capture_add`
(the manual/typed/voice path, A4) is unaffected by this key — a human
typing "nag ..." into a live conversation has already made that call
themselves; this key only gates the unattended WhatsApp scan. `feedback`
carries none of `nag`'s social-risk baggage, so — unlike `nag` — it ships
in the default set (Wave 2 N4).

`config_schema.feedback_recipient_user` (Wave 2 N4, 2026-09-04): who a
`feedback`-kind capture notifies, by user `name` (e.g. `"alex"`), resolved
at runtime rather than a hardcoded user id — see
`tools.py::_resolve_feedback_recipient`. Not `required`: an unconfigured
household still gets a sensible recipient (see that function's fallback),
it just can't be pointed anywhere but the fallback until set.
"""

from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest

MANIFEST = IntegrationManifest(
    name="household",
    display_name="Household Domains",
    version="1.3.0",
    type="capability",
    description=(
        "Household domains — standing areas of end-to-end responsibility "
        "(bins, dishwasher, laundry, toilets), each owned by exactly one "
        "person — plus a per-sender capture inbox for task/nag/discuss/"
        "surface/feedback-shaped messages awaiting review (which keywords "
        "the WhatsApp scan watches for is configurable; see "
        "capture_keywords). 'feedback' captures notify a configured "
        "recipient directly rather than only awaiting review. DB is the "
        "source of truth; no scheduled sync."
    ),
    icon="Home",
    models=["Domain", "DomainCheck", "HouseholdCapture", "HouseholdCaptureSourceMessage"],
    embedding_sources=[],
    reads_from=[],
    writes_to=[],
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={
        "capture_keywords": ConfigFieldSpec(
            type="list_str",
            default=["task", "discuss", "surface", "feedback"],
            description=(
                "Which capture keywords the WhatsApp scan (household_"
                "capture_capture) watches for, leading a message ("
                "'Nag, Tupperware drawer', 'Surface: bins', 'Feedback: "
                "the loops app is slow'). Must be a subset of task/nag/"
                "discuss/surface/feedback. Default excludes 'nag': "
                "household-ops-and-loops-2026-08.md's open decision 6 "
                "flags nag as socially risky and undecided, so enabling "
                "it here is a deliberate household choice, not a build. "
                "'feedback' carries no such risk and ships on by default. "
                "Does not affect household_capture_add (manual/typed/"
                "voice capture), which accepts any kind."
            ),
        ),
        "feedback_recipient_user": ConfigFieldSpec(
            type="str", required=False,
            description=(
                "User name a 'feedback'-kind capture notifies (e.g. "
                "'alex') — 'this is hard/flaky/broken' reports route here "
                "first for triage, never broadcast household-wide. "
                "Unset falls back to the household's first active member "
                "by id; see tools.py::_resolve_feedback_recipient."
            ),
        ),
    },
    oauth=None,
    # Nothing consumes a `household.*` capability yet — no facade
    # speculatively declared as `provides` (writing-an-integration.md §6).
    # `facade.py` still exists as a ready seam for a future consumer (see
    # its own docstring) without pretending one exists today.
    provides=[],
    depends_on=["notify.push"],
)
