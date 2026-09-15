"""Manifest for the `_template` scaffold integration — V4 chunk 4.3e.

Read this file alongside `server/docs/writing-an-integration.md`, which walks
through every field below in prose. This module only carries the mechanical
detail: what to put in each field, and why.

Why `_template` (leading underscore): `app.plugin.discovery.discover_integrations()`
and `app.plugin.validate.discover_manifests()` both skip any `app/integrations/*`
directory whose name starts with `_` — see their `subdir.name.startswith("_")`
checks. That means this package is *never* imported, registered, scheduled, or
validated in a real run of the server. It exists purely to be copied: either by
a human starting a new integration (copy the directory, rename it, `sed` out
the placeholder), or by `tests/test_drop_in_integration.py`, which does exactly
that programmatically as the project's north-star acceptance proof — "drop a
package + manifest + config, touch zero kernel files, and the kernel picks it
up."

`__TEMPLATE_INTEGRATION_NAME__` is a literal placeholder string, not a Python
f-string or template — search-and-replace it (across every file in this
package) with your new integration's real name before you use this as a
starting point. It appears in exactly two places that MUST agree with the
package's own directory name: `MANIFEST.name` below, and the `name` property
on the `BaseIntegration` subclass in `__init__.py`. Both are checked at
startup by `app.plugin.validate.validate_manifests()` — a mismatch fails boot
loudly rather than silently misregistering.
"""

from app.plugin.manifest import (
    ConfigFieldSpec,
    IntegrationManifest,
    StalenessProbe,
)

MANIFEST = IntegrationManifest(
    # --- Identity ---------------------------------------------------------
    # Must equal this package's own directory name under app/integrations/,
    # and must equal the `name` property returned by the BaseIntegration
    # subclass in __init__.py. `app.plugin.validate._check_matches_abc` and
    # `validate_manifests`'s own `manifest.name != name` check both enforce
    # this at boot.
    name="__TEMPLATE_INTEGRATION_NAME__",
    display_name="Template Integration",  # human-readable — shown in the dashboard, no uniqueness constraint
    version="1.0.0",  # every integration starts at 1.0.0; bump on a breaking internal change (decision 7, V4 index)
    # --- Type ---------------------------------------------------------
    # One of: "source" | "push_source" | "bidirectional" | "action" |
    # "capability" | "system". This drives which typed base class
    # (`app/plugin/bases.py`) you subclass in __init__.py — nothing enforces
    # the two match today (a couple of real integrations' `type` and base
    # class disagree slightly, e.g. attachments — see 4.3's batch notes), but
    # keeping them aligned is the honest default. This scaffold is a
    # `SourceIntegration` (poll-a-remote-API-on-a-schedule) — the single most
    # common shape. If your new integration is push-fed, bidirectional, a
    # bare action wrapper, or a capability service with no external system,
    # copy the shape of whatsapp/apple_reminders/irish_rail/coffee instead —
    # see `server/docs/writing-an-integration.md`'s "sync contract" section
    # for how to choose.
    type="source",
    description="Scaffold integration — copy this package to start a new one.",
    icon="Blocks",  # lucide-react icon component name (chunk 1.3); "Blocks" is a reasonable generic default
    # --- Data ownership -----------------------------------------------
    # Every ORM class this integration owns, by class name (string, not a
    # live reference — resolved lazily by
    # app.plugin.discovery.discover_integration_models() so app/models/__init__.py
    # never needs an import for it). Must match a class actually defined in
    # this package's own models.py — app.plugin.validate._check_models()
    # checks this at boot and fails loud on a typo.
    models=["TemplateItem"],
    # Source strings this integration feeds into the unified `embeddings` /
    # `embedding_queue` tables (app.services.embedding.EmbeddingService).
    # Must be globlally unique across every manifest — app.plugin.validate
    # ._check_unique_embedding_sources() enforces this. Leave `[]` (as here)
    # if this integration has no free-text content worth semantic search.
    embedding_sources=[],
    # --- External flows (security-relevant declaration, chunk 1.1) -----
    # What this integration talks to, in plain strings — not consumed by any
    # enforcement mechanism yet, but it's the one place a reviewer can grep
    # to answer "what does this package reach out to". Keep both lists
    # accurate even though nothing currently fails boot over them.
    reads_from=["template-external-api"],
    writes_to=[],
    # --- Scheduling (V4 chunk 3.1 — the manifest is the ONLY source of
    # truth for schedule; there is no ABC method to override anymore) -----
    # A crontab string, or None for an integration with nothing to poll on a
    # schedule (push-fed, action-only, or a pure capability service). This
    # scaffold syncs every 30 minutes as a placeholder — pick whatever cadence
    # your real external system's rate limits and data freshness needs
    # actually justify.
    schedule="*/30 * * * *",
    schedule_timezone=None,  # None = UTC; set an IANA zone string if the schedule needs to track local wall-clock time
    # Mirrors app/services/freshness.py's per-tool freshness gate (checked at
    # tool-dispatch time via ensure_fresh() — see app/plugin/dispatch.py).
    # None disables the freshness gate for this integration's tools.
    freshness_threshold_minutes=45,
    # Mirrors app/services/data_freshness.py's dashboard/system_alerts probe:
    # "is new data actually landing in the table" (distinct from "did the
    # sync job report success" — a job can succeed while producing nothing).
    # Most integrations describe this as "MAX(timestamp_column) on this
    # model, older than threshold_minutes is stale" — set `model` +
    # `timestamp_column` for that common case. Two real integrations don't
    # fit that shape and use `probe_function` instead — see
    # `StalenessProbe`'s own docstring in app/plugin/manifest.py for exactly
    # which ones and why (apple_reminders: bridge liveness off a core-table
    # column; homeassistant: a live in-process function, no column at all).
    staleness_probe=StalenessProbe(
        model="TemplateItem",
        timestamp_column="fetched_at",
        threshold_minutes=90,
    ),
    # Executable background tasks (startup-supervised long-runners, or extra
    # cron jobs beyond the main sync) — see `TaskSpec`'s docstring in
    # app/plugin/manifest.py for the "startup" vs "cron" kinds and their
    # exact function signatures. This scaffold declares none; whatsapp's
    # bridge-heartbeat cron task and homeassistant's WS-listener startup task
    # are the two real examples to copy from.
    background_tasks=[],
    # Dotted refs to APIRouter instances this integration wants mounted by
    # the kernel (push-fed ingest routes, OAuth callbacks, etc). None needed
    # here — see apple_health's manifest for a push-source example.
    routes=[],
    # --- Config (V4 chunk 3.3) ------------------------------------------
    # Every config key this integration reads via
    # `app.plugin.config_store.plugin_config(name)` — NOT read off the
    # kernel's `HomeSettings` singleton (that was deliberately trimmed to
    # kernel-only fields in chunk 3.3). One entry here is everything you
    # need to add a new config knob: no config.py edit, no env var
    # plumbing beyond the transition-period HOME_<KEY> fallback (see
    # config_store.py's module docstring).
    config_schema={
        "api_key": ConfigFieldSpec(
            type="str",
            required=True,  # is_configured() (BaseIntegration's default) is False until this is set
            secret=True,  # Fernet-encrypted at rest; the dashboard config UI masks it as "•••last4"
            description="API key for the template external service.",
        ),
        "page_size": ConfigFieldSpec(
            type="int",
            required=False,
            default=50,
            description="Records to fetch per page (optional, defaults to 50).",
        ),
    },
    # Set this if your integration needs a Google OAuth scope grant (see
    # google_calendar/google_mail's manifests) — None here, this scaffold's
    # "external API" is a plain bearer-token API, not Google.
    oauth=None,
    # --- Capability contract (V4 chunk 4.2) -----------------------------
    # `provides`: capability names this integration exposes to others via a
    # `facade.py` module-level `FACADE` singleton (see
    # app/plugin/capabilities.py). Leave empty unless another integration
    # genuinely needs to call into this one — don't add a facade
    # speculatively. `depends_on`: capability strings (not package names)
    # this integration itself consumes from someone else's `provides` — see
    # `system/tools.py` for a real consumer, and
    # `app.plugin.validate._check_dependency_graph` for what gets enforced
    # (every entry must resolve to a real provider; the resulting graph must
    # be acyclic). Both empty here — this scaffold neither offers nor needs
    # a capability.
    provides=[],
    depends_on=[],
)
