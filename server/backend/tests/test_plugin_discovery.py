"""Auto-discovery + annotation-registration tests — V4 chunk 1.2 (unit tier).

Covers:
  - `app.plugin.discovery.discover_integrations()` finds exactly the 24
    known integrations (19 since chunk 1.2, `embedding` since chunk 3.4
    turned it into a real capability package, `sheets` since chunk 4.2
    turned it into a real CapabilityService, and `notifications` since
    2026-07-31 gave system alerts a push sink, and `transcription`
    the same day for audio-to-text), in deterministic
    (sorted-by-name) order, and `register_all()` (now a thin wrapper over
    it) registers the same set.
  - `app.plugin.discovery.discover_integration_models()` returns the same
    model classes the old hand-maintained `app/models/__init__.py` import
    block used to list.
  - Every currently-registered MCP tool carries its own inline
    `annotations`, and that mapping is byte-identical to the old, now-
    deleted, `app/mcp/annotations.py::TOOL_ANNOTATIONS` centralized dict
    (kept here as a literal — frozen via `git show` of the pre-deletion
    file — so this test doesn't (and, post-deletion, can't) import that
    module). This is both the equivalence proof for the migration and the
    ongoing "every tool has annotations" regression guard.
"""

from app.integrations import INTEGRATIONS, get_all, register_all
from app.plugin.discovery import discover_integration_models, discover_integrations

EXPECTED_INTEGRATION_NAMES = {
    "apple_health",
    "apple_reminders",
    "attachments",
    "coffee",
    "commute",
    "embedding",
    "finance",
    "google_calendar",
    "google_docs",
    "google_mail",
    "historical_corpus",
    "homeassistant",
    "household",
    "inbox",
    "irish_rail",
    "lastfm",
    "media",
    "notifications",
    "obsidian",
    "sheets",
    "snags",
    "tasks",
    "solar_forecast",
    "strava",
    "system",
    "transcription",
    "vision",
    "weather",
    "whatsapp",
}

# Frozen literal of the old, now-deleted app/mcp/annotations.py::TOOL_ANNOTATIONS,
# exactly as merged at registration time (`tool_def.get("annotations") or
# TOOL_ANNOTATIONS.get(name)`) before this chunk. Every tool named here must
# still resolve to inline annotations equal to the value shown — deliberately
# NOT sourced from a live import of the deleted module.
_READ = {"readOnlyHint": True, "idempotentHint": True}
_READ_LIVE = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}
_WRITE_APPEND = {"readOnlyHint": False, "idempotentHint": False}
_WRITE_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}
_WRITE_IDEMPOTENT = {"readOnlyHint": False, "idempotentHint": True}

OLD_TOOL_ANNOTATIONS: dict[str, dict] = {
    "health_today": _READ,
    "health_sleep": _READ,
    "health_workouts": _READ,
    "health_trends": _READ,
    "health_summary": _READ,
    "health_exercise_status": _READ,
    "health_weekly_summary": _READ,

    "reminders_list": _READ,
    "reminders_lists": _READ,
    "reminders_sync": {"readOnlyHint": False, "idempotentHint": True},
    "reminders_sync_backlog": {"readOnlyHint": False, "idempotentHint": True},

    "inbox_pending": _READ,
    "inbox_preview": _READ,
    "inbox_to_vault": _WRITE_APPEND,
    "inbox_to_corpus": _WRITE_APPEND,
    "inbox_archive": {"readOnlyHint": False, "idempotentHint": True},
    "inbox_dismiss": {"readOnlyHint": False, "idempotentHint": True},

    "attachments_search": _READ,
    "attachments_pending": _READ,
    "attachments_scan": _WRITE_IDEMPOTENT,
    "attachments_ingest": _WRITE_APPEND,

    "snag_list": _READ,
    "snag_capture": _WRITE_IDEMPOTENT,
    "snag_add": _WRITE_APPEND,
    "snag_update": {"readOnlyHint": False, "idempotentHint": True},
    "snag_render": _WRITE_IDEMPOTENT,

    "media_recent": _READ,
    "media_sync": _WRITE_IDEMPOTENT,
    "media_fetch": _WRITE_IDEMPOTENT,
    "media_export": _WRITE_APPEND,

    "coffee_log": _WRITE_APPEND,
    "coffee_brew": _WRITE_APPEND,
    "coffee_delete_brew": _WRITE_DESTRUCTIVE,
    "coffee_rate": {"readOnlyHint": False, "idempotentHint": True},
    "coffee_recommend": _READ,
    "coffee_current": _READ,
    "coffee_similar": _READ,
    "coffee_embed": _WRITE_IDEMPOTENT,
    "coffee_dial_in": _READ,

    "finance_summary": _READ,
    "finance_transactions": _READ,
    "finance_categories": _READ,
    "finance_trends": _READ,
    "finance_subscriptions": _READ,
    "finance_top_merchants": _READ,
    "finance_compare": _READ,
    "finance_accounts": _READ,
    "finance_uncategorized": _READ,
    "finance_import_csv": _WRITE_APPEND,
    "finance_register_fingerprint": _WRITE_APPEND,
    "finance_add_rule": _WRITE_APPEND,

    "calendar_list_events": _READ_LIVE,
    "calendar_today": _READ_LIVE,
    "calendar_next_events": _READ_LIVE,
    "calendar_create_event": {"readOnlyHint": False, "openWorldHint": True},

    "gmail_unread": _READ_LIVE,
    "gmail_search": _READ,
    "gmail_recent": _READ,
    "gmail_thread": _READ_LIVE,
    "gmail_backfill": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "gmail_semantic_search": _READ,
    "gmail_stats": _READ,
    "gmail_embed": _WRITE_IDEMPOTENT,

    "ha_home_status": _READ,
    "ha_entity": _READ,
    "ha_entities": _READ,
    "ha_history": _READ,
    "ha_events": _READ,

    "commute_status": _READ,
    "commute_history": _READ,
    "commute_query": _READ_LIVE,

    "corpus_search": _READ,
    "corpus_stats": _READ,

    "rail_departures": _READ_LIVE,
    "rail_next": _READ_LIVE,

    "lastfm_recent": _READ,
    "lastfm_search": _READ,
    "lastfm_stats": _READ,
    "lastfm_backfill": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "lastfm_enrich": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},

    "vault_search": _READ,
    "vault_recent": _READ,
    "vault_stats": _READ,

    "system_alerts": _READ,
    "system_morning_briefing": _READ,
    "system_week_ahead": _READ,
    "system_search_everything": _READ,

    "weather_current": _READ_LIVE,
    "weather_forecast": _READ_LIVE,

    "whatsapp_recent": _READ,
    "whatsapp_search": _READ,
    "whatsapp_thread": _READ,
    "whatsapp_contacts": _READ,
    "whatsapp_semantic_search": _READ,
    "whatsapp_embed": _WRITE_IDEMPOTENT,
    "whatsapp_stats": _READ,

    "search_semantic": _READ,
    "search_stats": _READ,
}

# `vault_transfer` didn't exist in the old TOOL_ANNOTATIONS dict at all (a
# pre-existing gap — it shipped with no inline annotations override and no
# centralized-dict entry, so it silently registered with no annotations
# before this chunk). This chunk's raise-if-missing rule means it needs one
# now; classified here as a non-destructive, non-idempotent write (it moves
# a file out of the sender's vault), matching the `inbox_to_vault`/
# `media_export` convention for "writes that move/insert files".
NEW_TOOLS_ADDED_ANNOTATIONS = {
    "vault_transfer": {"readOnlyHint": False, "idempotentHint": False},
}


class TestDiscovery:
    def test_discover_integrations_matches_expected_29(self):
        instances = discover_integrations()
        names = [i.name for i in instances]
        assert set(names) == EXPECTED_INTEGRATION_NAMES
        assert len(names) == 29
        assert names == sorted(names), "discovery order must be deterministic (sorted by name)"

    def test_register_all_populates_same_set(self):
        INTEGRATIONS.clear()
        register_all()
        assert set(get_all().keys()) == EXPECTED_INTEGRATION_NAMES

    def test_discover_integration_models_nonempty_and_resolves(self):
        models = discover_integration_models()
        assert "CalendarEvent" in models
        assert "VaultChunk" in models
        assert "Snag" in models
        assert "MediaItem" in models
        # Every returned value really is the class it claims to be.
        for cls_name, cls in models.items():
            assert cls.__name__ == cls_name


class TestAnnotationsEquivalence:
    def _all_registered_tools(self) -> dict[str, dict]:
        """tool_name -> tool_def dict, across every registered integration."""
        INTEGRATIONS.clear()
        register_all()
        tools: dict[str, dict] = {}
        for integration in get_all().values():
            for tool_def in integration.mcp_tools():
                tools[tool_def["name"]] = tool_def
        return tools

    def test_every_tool_has_inline_annotations(self):
        tools = self._all_registered_tools()
        missing = [name for name, t in tools.items() if not t.get("annotations")]
        assert missing == [], f"tools with no inline annotations: {missing}"

    def test_annotations_match_old_centralized_dict(self):
        tools = self._all_registered_tools()
        expected = {**OLD_TOOL_ANNOTATIONS, **NEW_TOOLS_ADDED_ANNOTATIONS}
        checked = 0
        for name, ann in expected.items():
            if name not in tools:
                # Client-side-only reminders tools (add/complete/update) are
                # never registered server-side — same as before this chunk.
                continue
            assert tools[name]["annotations"] == ann, (
                f"{name}: inline annotations {tools[name]['annotations']} != "
                f"old TOOL_ANNOTATIONS value {ann}"
            )
            checked += 1
        assert checked > 60, "sanity check: expected to verify the bulk of the old dict"
