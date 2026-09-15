from app.plugin.manifest import IntegrationManifest

MANIFEST = IntegrationManifest(
    name="sheets",
    display_name="Google Sheets Export",
    version="1.0.0",
    type="capability",
    description="Generic 'push a table of rows to a Google Sheet' writer for household members without MCP/vault access.",
    icon="Table",
    models=["SheetExport"],
    embedding_sources=[],
    reads_from=[],
    writes_to=["google-sheets-api", "google-drive-api"],
    schedule=None,  # invoked by other integrations, never scheduled itself
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={},
    # Sheets/Drive scopes are requested against the CALLING user's own Google
    # account (since 2026-09-06 — `snags` passes the caller's account; the
    # configured owner account is gone, the same rule PR #122 applied to
    # Google Docs). This integration doesn't own an account itself, so it
    # declares no OAuthRequirement of its own; callers pass an
    # owner_account_email/owner_user_id explicitly (see facade.py), and the
    # consumer's own manifest declares the scopes it needs.
    oauth=None,
    provides=["sheets.write"],  # facade: app.integrations.sheets.facade — consumed by snags
    depends_on=[],
)
