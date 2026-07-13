"""Centralized MCP tool annotations.

Tool dicts can declare their own `annotations` key (preferred), but this
dict provides defaults keyed by tool name so every existing hand-written
tool gets sensible hints without touching 12 different `tools.py` files.

Annotations the MCP spec defines (all advisory):
- `readOnlyHint`     : the tool only reads — never mutates state
- `destructiveHint`  : the tool may delete or destroy data
- `idempotentHint`   : calling twice with the same args is safe
- `openWorldHint`    : the tool reaches an external API / network

Convention used here:
- Pure DB reads / list / search / stats / get → readOnlyHint + idempotentHint
- External-API reads (Gmail live, weather, Last.fm fetch) → +openWorldHint
- Backfills / embed jobs → idempotent (safe to re-run) but not read-only
- Writes that overwrite existing rows → destructiveHint=True
- Writes that append/insert new rows → idempotentHint=False
- Side-effect-free composed reports (morning briefing, weekly summary) → readOnlyHint=True
"""

# Helpers — the four common shapes
_READ = {"readOnlyHint": True, "idempotentHint": True}
_READ_LIVE = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}
_WRITE_APPEND = {"readOnlyHint": False, "idempotentHint": False}
_WRITE_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}
_WRITE_IDEMPOTENT = {"readOnlyHint": False, "idempotentHint": True}


TOOL_ANNOTATIONS: dict[str, dict] = {
    # ---- Apple Health (all reads from local DB) ----
    "health_today": _READ,
    "health_sleep": _READ,
    "health_workouts": _READ,
    "health_trends": _READ,
    "health_summary": _READ,
    "health_exercise_status": _READ,
    "health_weekly_summary": _READ,

    # ---- Apple Reminders (writes happen on the local client; server-side reads) ----
    "reminders_list": _READ,
    "reminders_lists": _READ,
    "reminders_sync": {"readOnlyHint": False, "idempotentHint": True},
    "reminders_sync_backlog": {"readOnlyHint": False, "idempotentHint": True},
    # Local-only client-side tools (never registered server-side, included for completeness):
    "reminders_add": _WRITE_APPEND,
    "reminders_complete": {"readOnlyHint": False, "idempotentHint": True},
    "reminders_update": {"readOnlyHint": False, "idempotentHint": True},

    # ---- Inbox (webhook drop-zone triage) ----
    "inbox_pending":     _READ,                 # may inline-enrich sidecars (idempotent)
    "inbox_preview":     _READ,
    "inbox_to_vault":    _WRITE_APPEND,          # copies file + archives source
    "inbox_to_corpus":   _WRITE_APPEND,          # ingests into historical_documents
    "inbox_archive":     {"readOnlyHint": False, "idempotentHint": True},
    "inbox_dismiss":     {"readOnlyHint": False, "idempotentHint": True},

    # ---- Attachments ----
    "attachments_search": _READ,
    "attachments_pending": _READ,
    "attachments_scan": _WRITE_IDEMPOTENT,    # scans + queues; safe to re-run
    "attachments_ingest": _WRITE_APPEND,       # ingests + creates rows

    # ---- Snag register (DB source of truth; vault note generated) ----
    "snag_list":    _READ,
    "snag_capture": _WRITE_IDEMPOTENT,         # already-captured messages skipped
    "snag_add":     _WRITE_APPEND,
    "snag_update":  {"readOnlyHint": False, "idempotentHint": True},
    "snag_render":  _WRITE_IDEMPOTENT,         # regenerates the same view

    # ---- Media store (WhatsApp images/videos/audio) ----
    "media_recent": _READ,
    "media_sync":   _WRITE_IDEMPOTENT,         # scan + window download; safe to re-run
    "media_fetch":  _WRITE_IDEMPOTENT,         # re-download by id; same bytes → same file
    "media_export": _WRITE_APPEND,             # copies files into the vault

    # ---- Coffee (mostly DSL-managed; non-DSL writes here) ----
    "coffee_log":         _WRITE_APPEND,       # logs a brew (insert)
    "coffee_brew":        _WRITE_APPEND,       # records a brew session
    "coffee_delete_brew": _WRITE_DESTRUCTIVE,
    "coffee_rate":        {"readOnlyHint": False, "idempotentHint": True},
    "coffee_recommend":   _READ,
    "coffee_current":     _READ,
    "coffee_similar":     _READ,
    "coffee_embed":       _WRITE_IDEMPOTENT,
    "coffee_dial_in":     _READ,

    # ---- Finance ----
    "finance_summary":               _READ,
    "finance_transactions":          _READ,
    "finance_categories":            _READ,
    "finance_trends":                _READ,
    "finance_subscriptions":         _READ,
    "finance_top_merchants":         _READ,
    "finance_compare":               _READ,
    "finance_accounts":              _READ,
    "finance_uncategorized":         _READ,
    "finance_import_csv":            _WRITE_APPEND,
    "finance_register_fingerprint":  _WRITE_APPEND,
    "finance_add_rule":              _WRITE_APPEND,

    # ---- Google Calendar ----
    "calendar_list_events":  _READ_LIVE,       # live API
    "calendar_today":        _READ_LIVE,
    "calendar_next_events":  _READ_LIVE,
    "calendar_create_event": {"readOnlyHint": False, "openWorldHint": True},

    # ---- Google Mail ----
    "gmail_unread":            _READ_LIVE,
    "gmail_search":            _READ,
    "gmail_recent":            _READ,
    "gmail_thread":            _READ_LIVE,
    "gmail_backfill":          {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "gmail_semantic_search":   _READ,
    "gmail_stats":             _READ,
    "gmail_embed":             _WRITE_IDEMPOTENT,

    # ---- Home Assistant (cached in Postgres; the 5-min sync is what's open-world) ----
    "ha_home_status": _READ,
    "ha_entity":      _READ,
    "ha_entities":    _READ,
    "ha_history":     _READ,

    # ---- Historical corpus ----
    "renovation_context": _READ,
    "corpus_stats":    _READ,

    # ---- Irish Rail (live API) ----
    "rail_departures": _READ_LIVE,
    "rail_next":       _READ_LIVE,

    # ---- Last.fm (DSL) ----
    "lastfm_recent":    _READ,
    "lastfm_search":    _READ,
    "lastfm_stats":     _READ,
    "lastfm_backfill":  {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "lastfm_enrich":    {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},

    # ---- Obsidian / vault ----
    # Read/write the vault via the client's native filesystem tools; only the
    # search/index tools are exposed over MCP.
    "vault_search": _READ,
    "vault_recent": _READ,
    "vault_stats":  _READ,

    # ---- System (cross-source reports) ----
    "system_alerts":              _READ,
    "system_morning_briefing":    _READ,
    "system_week_ahead":          _READ,
    "system_search_everything":   _READ,

    # ---- Weather ----
    "weather_current":  _READ_LIVE,
    "weather_forecast": _READ_LIVE,

    # ---- WhatsApp ----
    "whatsapp_recent":           _READ,
    "whatsapp_search":           _READ,
    "whatsapp_thread":           _READ,
    "whatsapp_contacts":         _READ,
    "whatsapp_semantic_search":  _READ,
    "whatsapp_embed":            _WRITE_IDEMPOTENT,
    "whatsapp_stats":            _READ,
}
