"""Sync-on-read freshness threshold tests (unit tier).

V4 chunk 1.3: `app.services.freshness.FRESHNESS_THRESHOLDS` (a hand-maintained
name-keyed dict) was replaced by a manifest-driven lookup
(`threshold_seconds()` / `ensure_fresh()`), sourced from each integration's
own `manifest.py::freshness_threshold_minutes`. This test pins the new
lookup against a frozen copy of the old dict so a manifest edit that
silently changes freshness behavior fails here rather than shipping quietly.
"""

from app.services import freshness


# Frozen fixture — the old FRESHNESS_THRESHOLDS dict, verbatim, before it was
# deleted in this chunk. Not a live import of deleted code.
OLD_FRESHNESS_THRESHOLDS: dict[str, int | None] = {
    "google_calendar": 120,
    "google_mail": 120,
    "whatsapp": 60,
    "apple_reminders": 120,
    "weather": 1800,
    "lastfm": 900,
    "obsidian": 600,
    "finance": None,
    "irish_rail": None,
    "embedding": None,
}

# All 19 integration package names (chunk 1.1's manifest sweep), whether or
# not they appeared in the old dict. Absent-from-the-old-dict == None
# (never auto-freshen) is the behavior under test for the other 9.
ALL_INTEGRATION_NAMES = {
    "apple_health",
    "apple_reminders",
    "attachments",
    "coffee",
    "commute",
    "finance",
    "google_calendar",
    "google_mail",
    "historical_corpus",
    "homeassistant",
    "inbox",
    "irish_rail",
    "lastfm",
    "media",
    "obsidian",
    "snags",
    "system",
    "weather",
    "whatsapp",
}


class TestThresholdSecondsMatchesOldDict:
    def test_all_19_integrations_match_old_dict_behavior(self):
        assert len(ALL_INTEGRATION_NAMES) == 19
        for name in ALL_INTEGRATION_NAMES:
            expected = OLD_FRESHNESS_THRESHOLDS.get(name)  # None if absent
            actual = freshness.threshold_seconds(name)
            assert actual == expected, (
                f"{name}: manifest-driven threshold {actual!r} != "
                f"old dict behavior {expected!r}"
            )

    def test_unknown_integration_name_is_never_freshened(self):
        # "embedding" isn't a registered integration (no manifest); the old
        # dict explicitly mapped it to None, and any other unknown name
        # must behave the same way via .get() fallback.
        assert freshness.threshold_seconds("embedding") is None
        assert freshness.threshold_seconds("not-a-real-integration") is None
