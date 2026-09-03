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
    # Sheets/Drive scopes are requested against whichever Google account is
    # configured as an owner (currently only `snags`' HOME_SHEETS_OWNER_ACCOUNT
    # — see that integration's own manifest/oauth). This integration doesn't
    # own an account itself, so it declares no OAuthRequirement of its own;
    # the *credential delegation* mechanism (which account's token a given
    # export call is authorized to use, and how that authorization is
    # declared rather than just "whatever HOME_SHEETS_OWNER_ACCOUNT says") is
    # V4 chunk 2.4's job — deliberately on hold pending sam-rollout Phase
    # B-D. For now this stays exactly the pre-4.2 behavior: callers pass an
    # owner_account_email/owner_user_id explicitly (see facade.py).
    oauth=None,
    provides=["sheets.write"],  # facade: app.integrations.sheets.facade — consumed by snags
    depends_on=[],
)
