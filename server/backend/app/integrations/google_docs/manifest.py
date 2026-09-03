from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, OAuthRequirement

MANIFEST = IntegrationManifest(
    name="google_docs",
    display_name="Google Docs",
    version="1.0.0",
    # "action", not "capability" (which is what `sheets` is): this integration
    # has MCP tools of its own that hit the Docs/Drive APIs directly from the
    # tool handler. There is nothing to poll and no cached copy of a document
    # in Postgres — `doc_exports` is a registry of documents comar owns, not a
    # mirror of their contents.
    type="action",
    description="Read, write and edit Google Docs — whole-document writes from markdown, plus append and find/replace.",
    icon="FileText",
    models=["DocExport"],
    embedding_sources=[],
    reads_from=["google-docs-api", "google-drive-api"],
    writes_to=["google-docs-api", "google-drive-api"],
    schedule=None,  # nothing to poll — every call is user- or caller-initiated
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={
        # Deliberately NOT required=True. `required` gates the whole
        # integration off via is_configured(), and most of this integration's
        # surface (read/append/replace against a document id) works fine
        # without an owner account configured. The two calls that genuinely
        # need it — creating and whole-document overwriting — raise a
        # PermanentError naming the missing key instead. See the root
        # CLAUDE.md's "required config is not a fit when read paths still
        # work" rule.
        #
        # Both keys are prefixed `docs_` on purpose. `config_store.plugin_config`
        # derives the env fallback as `HOME_<KEY>`, so a bare `owner_account`
        # would claim the global name `HOME_OWNER_ACCOUNT` — generic enough that
        # a future integration would silently collide with it. `snags` prefixes
        # for the same reason (`HOME_SHEETS_OWNER_ACCOUNT`).
        "docs_owner_account": ConfigFieldSpec(
            type="str", required=False,
            description="Google account whose OAuth token owns/writes documents comar creates.",
        ),
        "docs_share_with": ConfigFieldSpec(
            type="list_str", default=[],
            description="Emails to share newly created documents with, as writers.",
        ),
    },
    oauth=OAuthRequirement(
        provider="google",
        scopes=[
            # documents: read + batchUpdate on any doc the account can see.
            # This is what makes read/append/replace work on a document a
            # human created by hand.
            "https://www.googleapis.com/auth/documents",
            # drive.file: create, share and *replace the body of* files this
            # app created. Already in the union via `snags` (the Sheets
            # export), so it is not a new grant.
            #
            # ⚠️ drive.file is per-file, not account-wide, which is exactly
            # why whole-document overwrite (writer.py::write_markdown, a
            # Drive files.update) only works on docs comar created, while
            # append/replace (Docs API) work on any doc. Widening this to
            # .../auth/drive would lift that limit at the cost of full Drive
            # access for every Google integration sharing this scope union —
            # not a trade made here.
            "https://www.googleapis.com/auth/drive.file",
        ],
    ),
    provides=["docs.write"],  # facade: app.integrations.google_docs.facade
    depends_on=[],
)
