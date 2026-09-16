from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, OAuthRequirement

MANIFEST = IntegrationManifest(
    name="snags",
    display_name="Snag Register",
    version="1.0.0",
    type="capability",
    description="House snag register (DB is source of truth); mirrors into the vault note and a shared Google Sheet.",
    icon="ClipboardList",
    models=["Snag", "SnagMedia", "SnagSourceMessage"],
    embedding_sources=[],
    reads_from=[],
    writes_to=["google-sheets-api"],
    schedule=None,  # user-gated capture, no scheduled sync
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={
        # `sheets_owner_account` was removed 2026-09-06: the Sheets mirror is
        # written with the CALLING user's own Google token (every path in is
        # a tool call), the same rule PR #122 applied to Google Docs. A stale
        # row in `integration_config` is ignored.
        "sheets_share_with": ConfigFieldSpec(
            type="list_str", default=[],
            description="Emails to share new Sheets exports with.",
        ),
        # --- Deployment-specific vocabulary (2026-07-28) -------------------
        # These two used to be hardcoded Python constants naming this
        # household's rooms and contractors. They are per-deployment data,
        # not platform behaviour, so they now live in `integration_config`
        # (or the HOME_ROOM_ALIASES / HOME_TRADES env fallback).
        "room_aliases": ConfigFieldSpec(
            type="dict_str_str", default={},
            description=(
                "Maps how people actually type a room in a snag message to "
                "its canonical name, e.g. {\"utility\": \"Utility room\"}. "
                "Keys are matched lowercased. Unmapped rooms are just "
                "capitalised."
            ),
        ),
        "trades": ConfigFieldSpec(
            type="list_str", default=[],
            description=(
                "Trade/contractor vocabulary offered for new snags. Empty "
                "falls back to vocab.py::DEFAULT_TRADES. Safe to narrow: "
                "validation unions this with the trades already present in "
                "the table (vocab.allowed_trades), so existing rows never "
                "become un-editable."
            ),
        ),
        "trade_labels": ConfigFieldSpec(
            type="dict_str_str", default={},
            description=(
                "Display labels per trade slug for the rendered vault note "
                "and Sheet, e.g. {\"plumber\": \"Plumber (Dave)\"}. Missing "
                "entries fall back to a title-cased slug."
            ),
        ),
    },
    # The Sheets export needs spreadsheets+drive.file scopes on the calling
    # user's Google account — declared here since `sheets` itself has no
    # OAuth requirement of its own and snags is its one consumer.
    oauth=OAuthRequirement(
        provider="google",
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ],
    ),
    # `sheets` is now a proper CapabilityService (V4 chunk 4.2) providing
    # "sheets.write" — listed here alongside media.store.
    depends_on=["media.store", "sheets.write"],
    # `snags.query` (R5, absence detection, 2026-09-04): a thin read surface
    # so `tasks/absence.py::unanswered_snags` can find snags stuck without a
    # trade response, without importing `snags.models` directly (see
    # `facade.py`). `snags` depends on nothing that depends on `tasks`, so
    # this direction is safe — see `tasks`' manifest for why the dependency
    # cannot run the other way.
    provides=["snags.query"],
)
