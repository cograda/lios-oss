"""Single source of truth for lios' privacy model: which tables are
per-user (`UserOwnedMixin`), which per-user-looking tables are deliberately
NOT scoped (the nullable/admin allowlist), and which tables are shared
across the household by design.

This lives in app code, not test code, so it actually IS the source of
truth rather than a copy a test happens to agree with. Two consumers:

  - `tests/test_user_scoping.py` — the enforcement suite (leak-canary
    sweep, meta-test pinning classification exhaustiveness).
  - `app/integrations/system/tools.py::system_what_lios_sees` — the
    per-user "what does lios hold about me" tool (N5).

Keeping the classification here means a new table only needs one decision
made once, in one place, and both the test and the tool see it.
"""

from __future__ import annotations

from app.mixins import UserOwnedMixin

# ---------------------------------------------------------------------------
# Models with a user_id column that deliberately do NOT take UserOwnedMixin.
# ---------------------------------------------------------------------------
#
# Embedding/EmbeddingQueue: nullable user_id, NULL = household-shared,
# scoped inside EmbeddingService.search. InstallCode: admin-issued
# onboarding artefact, user_id records who the install is FOR.
NULLABLE_OR_ADMIN_USER_ID = {
    "embeddings", "embedding_queue", "install_codes",
    # auth_events: nullable user_id by design (a failed-auth attempt usually
    # has no resolved user) — admin/ops audit log, not per-user app data.
    # See app/models/auth_events.py's docstring (V4 chunk 2.5).
    "auth_events",
    # sync_cursors: nullable user_id by design — single-account integrations
    # (weather, lastfm's global sync) have no owning user for their cursor.
    # Opt-in bookkeeping table, not per-user app data. See
    # app/models/sync_cursor.py (V4 chunk 3.2).
    "sync_cursors",
    # notification_sends: nullable user_id by design (F7, hardening 2026-08) —
    # NULL means household-shared infrastructure alert, which is the NORMAL
    # case, not a backfill artifact; only user-attributable alerts (e.g. the
    # apple_health per-user data-gap) carry an owner. UserOwnedMixin's
    # NOT NULL would be wrong here. The read-side scoping (bound caller sees
    # shared + own, never another user's) is enforced directly by
    # tests/test_notifications.py::TestNotifyRecentScoping and the migration
    # docstring (2026_08_08_8b9c0d1e2f3a).
    "notification_sends",
    # runs: nullable user_id by design (S5.1; absorbed tool_calls Wave 5.1)
    # — most rows are a household-wide scheduled job with no bound caller
    # (the daily brief pre-warm, the routines tick, kernel prunes), but
    # kind="tool_call" rows DO carry a real per-caller user_id (ex-ToolCall).
    # Still on this allowlist rather than UserOwnedMixin because it's an
    # admin/ops audit trail sharing one table across both a scoped and an
    # unscoped kind — the read-side split (scheduled_job/manual rows never
    # filtered, tool_call rows filtered by caller) is enforced directly in
    # `app.services.runs.recent_activity`, not by this classification. See
    # app/models/runs.py.
    "runs",
}


# ---------------------------------------------------------------------------
# Tables whose per-user scoping column(s) are not the standard `user_id`
# (UserOwnedMixin's column). Table name -> the column name(s) that carry a
# user id; a table listed here is per-user for privacy purposes exactly like
# a UserOwnedMixin table, just spelled differently — `user_owned_models()`
# includes it, and `system_what_lios_sees` counts it per caller, via
# `user_columns_for()` below rather than a hand-written special case at each
# call site.
#
# A tuple of MORE than one column means the row is visible to a caller who
# matches ANY of them — that is a distinct scoping shape from "the row has
# one owner", and is exactly what `vault_read_grants` needs: a grant row is
# meaningfully "about" both the grantee (who gained read access) and the
# owner (whose vault was opened), and must be invisible to everyone else.
# `app/tools/helpers.py::scoped_query` and this module's callers OR the
# listed columns together rather than picking one.
#
# `vault_read_grants` was a deliberate, documented gap through PR #81 — a
# table matched by neither `user_id`/`owner_id` naming nor UserOwnedMixin,
# so the guard could not see it at all. Closed here (Wave 5.5): it is no
# longer exempted, it is classified.
USER_COLUMNS: dict[str, tuple[str, ...]] = {
    "vault_read_grants": ("grantee_user_id", "owner_user_id"),
}

_DEFAULT_USER_COLUMNS: tuple[str, ...] = ("user_id",)


def user_columns_for(model_or_table: type | str) -> tuple[str, ...]:
    """The column name(s) on `model_or_table` that carry a per-user id.

    Looks up `USER_COLUMNS` by table name first; falls back to the standard
    `("user_id",)` (UserOwnedMixin's column, and the right default for a
    table that isn't in the map at all — callers still guard with
    `hasattr`/`hasattr`-equivalent checks before using the name, so a
    household-shared table with no such column is unaffected).
    """
    table_name = (
        model_or_table if isinstance(model_or_table, str) else model_or_table.__tablename__
    )
    return USER_COLUMNS.get(table_name, _DEFAULT_USER_COLUMNS)


def user_owned_models() -> list[type]:
    """Every currently-live per-user model registered against `coglib.Base`
    — every `UserOwnedMixin` model, plus any table with a bespoke entry in
    `USER_COLUMNS` (e.g. `vault_read_grants`, scoped by `grantee_user_id`/
    `owner_user_id` rather than `user_id`).

    Filters to mappers whose table is *currently* a member of
    `Base.metadata.tables` — not just "still has a live Python class
    object". See `tests/test_drop_in_integration.py` for why a torn-down
    drop-in package's class can linger in `Base.registry.mappers` for a
    little while after its fixture's `with` block exits; the metadata
    membership check is what makes this safe to call mid-test-session.
    """
    from coglib import Base

    return sorted(
        (
            m.class_
            for m in Base.registry.mappers
            if m.local_table is not None
            and m.local_table.name in Base.metadata.tables
            and (
                issubclass(m.class_, UserOwnedMixin)
                or m.local_table.name in USER_COLUMNS
            )
        ),
        key=lambda c: c.__tablename__,
    )


# ---------------------------------------------------------------------------
# Household-shared tables: intentionally visible to BOTH users, and
# deliberately not UserOwnedMixin because there is no single owning user —
# one calendar, one house, one renovation, one task ledger. Table name ->
# one-line reason, so a viewer (or `system_what_lios_sees`) never has to
# infer "shared" from silence.
#
# This is NOT a completeness sweep of every non-user-owned table in the
# schema — kernel/infra tables (oauth_clients, sync_state, integration_config,
# ai_usage, the algo_* tables, ...) hold no data a person would recognise as
# "about them" and are deliberately left out of both this list and the tool's
# output. What belongs here is anything a caller might reasonably ask "is
# this mine or shared?" about. `vault_read_grants` used to be listed among
# the deliberately-excluded examples above — it no longer is, because it IS
# per-user data (see `USER_COLUMNS` below) and is classified there instead.
HOUSEHOLD_SHARED_TABLES: dict[str, str] = {
    # finance — one household ledger, not per-user
    "accounts": "Household finance — one shared ledger across both users.",
    "categories": "Household finance category taxonomy.",
    "categorization_rules": "Household finance auto-categorisation rules.",
    "transactions": "Household finance — one shared ledger across both users.",
    "import_history": "Household finance CSV import log.",
    "account_fingerprints": "Household finance dedup fingerprints.",
    "monthly_summaries": "Household finance monthly rollups.",
    # weather — one location
    "weather_current": "One location for the household — no per-user weather.",
    "weather_forecast": "One location for the household — no per-user weather.",
    # historical corpus — shared family archive, with one nullable owner
    # column. `owner_user_id` NULL (the default, and every source type but
    # one) = household-shared; a set owner = that user's alone, enforced by
    # embedding the document's chunks with `embeddings.user_id = owner` so
    # EmbeddingService.search excludes them for anyone else, plus a
    # document-level re-check in the corpus tools. Today the only owned
    # rows are `claude_conversation` (Alex's claude.ai export, 2026-09-06).
    "historical_documents": (
        "Shared family document archive (renovation paperwork, comms, manuals). "
        "Owner column nullable: NULL = household-shared; a set owner_user_id "
        "(today: claude_conversation) is private to that user."
    ),
    "historical_document_chunks": (
        "Shared family document archive, chunked for search — inherits the "
        "parent document's owner (NULL = household-shared)."
    ),
    # home assistant — one house
    "ha_entities": "One Home Assistant instance for the house.",
    "ha_state_changes": "One Home Assistant instance for the house.",
    # calendar
    "calendar_events": "Shared household calendar, by design.",
    # google docs / sheets export caches
    "doc_exports": "Shared Google Docs export cache.",
    "sheet_exports": "Shared Google Sheets export cache.",
    # snags (renovation)
    "snags": "Shared renovation snag register.",
    "snag_media": "Shared renovation snag register.",
    # household domains
    "domains": "Household life-domains register — both users must see it.",
    "domain_checks": "Household life-domains freshness log.",
    # commute
    "commute_decisions": "Unattended scheduler output — no `current_user_id()` context to scope to.",
    # tasks/routines — one shared backlog; `owner_id`/`pending_owner_id` are
    # assignment fields on a shared row, not a tenancy boundary
    "task_programs": "Shared task ledger — one household backlog.",
    "task_domain_tags": "Shared task ledger.",
    "task_projects": "Shared task ledger.",
    "tasks": "Shared task ledger — assignment (`owner_id`) is a field on the row, not row ownership.",
    "task_links": "Shared task ledger.",
    "task_comments": "Shared task ledger.",
    "task_events": "Shared task ledger.",
    "routines": "Shared task ledger.",
    "routine_steps": "Shared task ledger.",
    # shared reference/catalog data (not the private per-user activity log)
    "coffee_equipment_profiles": "Shared coffee equipment catalog, not the personal brew log.",
    "coffees": "Shared coffee/bean catalog, not the personal brew log.",
    "artist_tags": "Shared artist/genre reference data, not personal listening history.",
}


# ---------------------------------------------------------------------------
# Human labels
# ---------------------------------------------------------------------------

INTEGRATION_LABELS: dict[str, str] = {
    "google_mail": "Gmail",
    "whatsapp": "WhatsApp",
    "apple_health": "Health",
    "lastfm": "Music listening (Last.fm)",
    "coffee": "Coffee log",
    "apple_reminders": "Reminders",
    "obsidian": "Vault notes",
    "media": "Media",
    "attachments": "Message attachments (metadata only — see below)",
    "household": "Household capture inbox",
    "strava": "Strava activities",
    "inbox": "Inbox items",
    "system": "Account & app infrastructure (tokens, preferences, device logs)",
    "vault_read_grants": "Vault access grants (who can read your vault, and whose you can read)",
}


def integration_for_model(model: type) -> str:
    """Best-effort integration name for a model, derived from its module
    path rather than a hand-maintained mapping — `app.integrations.<name>.
    models` -> `<name>`; anything else (kernel `app/models/*`, e.g. tokens,
    client auth, preferences) -> `"system"`.
    """
    parts = model.__module__.split(".")
    if len(parts) >= 3 and parts[0] == "app" and parts[1] == "integrations":
        return parts[2]
    return "system"


def integration_for_table(table_name: str) -> str | None:
    """Same lookup as `integration_for_model`, but by table name — for
    tables (like the household-shared ones) where we don't necessarily
    have the model class handy at the call site."""
    from coglib import Base

    for mapper in Base.registry.mappers:
        if mapper.local_table is not None and mapper.local_table.name == table_name:
            return integration_for_model(mapper.class_)
    return None


def label_for(table_name: str, integration: str) -> str:
    """Human label for a table: an exact-table override, else the
    integration's label, else the integration name Title Cased — so a
    brand-new table still renders *something* sensible with zero edits
    here, rather than silently vanishing from the tool's output.
    """
    return (
        INTEGRATION_LABELS.get(table_name)
        or INTEGRATION_LABELS.get(integration)
        or integration.replace("_", " ").title()
    )


# Preference order mirrors tests/test_user_scoping.py's ListTool timestamp
# probe — created_at first (the common case), then a handful of
# integration-specific alternatives, else whatever DateTime column exists.
_TIMESTAMP_PREFERENCE = ("created_at", "received_at", "logged_at", "synced_at", "timestamp")


def timestamp_column_for(model: type) -> str | None:
    """Best DateTime column on `model` to report earliest/latest activity
    for, or None if it has none (some rows are pure join/junction tables)."""
    from sqlalchemy import DateTime
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    dt_cols = [c.name for c in mapper.columns if isinstance(c.type, DateTime)]
    for preferred in _TIMESTAMP_PREFERENCE:
        if preferred in dt_cols:
            return preferred
    return dt_cols[0] if dt_cols else None
