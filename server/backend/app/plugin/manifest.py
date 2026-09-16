"""Integration manifest schema — V4 chunk 1.1.

Every integration package under `app/integrations/<name>/` exports a
`manifest.py` with `MANIFEST = IntegrationManifest(...)` describing, in one
place, everything the kernel needs to know about it: the models it owns, the
embedding sources it feeds, the external systems it talks to, its schedule,
its freshness thresholds, its config keys, and its dependencies on other
integrations.

This chunk only *declares* the schema and writes accurate manifests for the
current tree — nothing consumes these yet (see `app/plugin/validate.py` for
the one startup consumer, which just double-checks the manifests are
internally consistent). Later V4 chunks (1.2+) replace the hand-maintained
kernel-side registries (models/__init__.py, freshness.py, data_freshness.py,
scheduler.py, routes/__init__.py, main.py background-task wiring) with code
that reads these manifests instead.

Zero behavior change: this module and the manifests it types are pure
declarations, imported by nothing at runtime except `validate.py`.
"""

from typing import Any, Literal

from pydantic import BaseModel


class StalenessProbe(BaseModel):
    """Mirrors one entry in `app/services/data_freshness.py::_probe()`.

    Most probes are "MAX(timestamp_column) on model, older than
    threshold_minutes is stale". A couple of integrations don't fit that
    shape:
      - `apple_reminders`'s probe is bridge liveness, keyed off
        `User.reminders_verified_at` — a core table, not one this
        integration owns. `model`/`timestamp_column` still describe it
        accurately; they just don't have to resolve inside the
        integration's own `models.py` (unlike the manifest's top-level
        `models` field, which validate.py does check that way).
      - `homeassistant`'s probe is a live in-process function
        (`ws_last_event_at()`), not a column at all — use `probe_function`
        instead of `model`/`timestamp_column` for cases like this.

    `per_user` changes the aggregation from one table-wide `MAX()` to one per
    owner. Without it, a probe over per-user data reports the *freshest* row
    across everybody, so one working device masks another's dead one — measured
    2026-08-17: `apple_reminders` read 0m (Alex's live daemon) while Sam's had
    been silent 46 minutes against a 5-minute threshold, and `apple_health` read
    5h (Sam's) while Alex's was 9h. Both were reported healthy.

    `user_column` is the column to group by. It defaults to `user_id`, which is
    right for anything using `UserOwnedMixin`; `apple_reminders` probes the core
    `User` table itself, where the owner column is `id`.
    """

    model: str | None = None
    timestamp_column: str | None = None
    probe_function: str | None = None  # dotted ref, e.g. "pkg.mod:func"
    threshold_minutes: int
    per_user: bool = False
    user_column: str = "user_id"

    #: Name of a key in this same integration's `config_schema` that, if set,
    #: overrides `threshold_minutes` at runtime — added 2026-08-27 so a probe
    #: threshold can be raised without a code change and without becoming a
    #: bare hardcoded constant either. None (the default, and every probe
    #: before this one) means the threshold is exactly `threshold_minutes`,
    #: forever, same as always. See `data_freshness.py::_effective_threshold_minutes`.
    threshold_config_key: str | None = None

    #: Restrict the probe to rows matching `filter_column == filter_value`.
    #: Needed when several integrations share one table, which is exactly the
    #: case for `type="deriver"` integrations: they all write to
    #: `algo_predictions`, so an unfiltered `MAX(made_at)` reports the freshest
    #: row across *every* deriver — one live forecaster masking a dead one.
    #: Structurally the same failure `per_user` exists to prevent (measured
    #: 2026-08-17: apple_reminders read 0m from Alex's live daemon while
    #: Sam's had been silent 46 minutes against a 5-minute threshold, and
    #: both reported healthy). Set it before the second deriver exists, not
    #: after.
    filter_column: str | None = None
    filter_value: str | None = None


class ConfigFieldSpec(BaseModel):
    """One key in an integration's `config_schema` (V4 chunk 3.3).

    Describes a single config value the integration reads — replaces the
    purely-descriptive `settings_keys: list[str]` from chunk 1.1. The kernel
    (`app.plugin.config_store.plugin_config()`) uses this to build a typed
    accessor whose values are sourced from the `integration_config` DB table,
    falling back to the like-named `HomeSettings` field during the
    transition period (one release, per the chunk 3.3 spec).

    `type` is one of "str" | "int" | "float" | "bool" | "list_str" |
    "dict_str_str" — the small set actually needed by the current
    integration config keys. "float" was added for R4 (retrieval recency
    decay half-life, a days value that is not meaningfully an int).
    """

    type: Literal["str", "int", "float", "bool", "list_str", "dict_str_str"] = "str"
    required: bool = False
    secret: bool = False
    default: Any = None
    description: str = ""


class OAuthRequirement(BaseModel):
    """Declares that an integration needs a Google OAuth scope grant.

    `app.auth.oauth` requests the union of every integration's `scopes` at
    consent time (V4 chunk 3.3) — replaces the single hard-coded `SCOPES`
    list that used to live in `app/auth/oauth.py`.
    """

    provider: Literal["google"]
    scopes: list[str]


class TaskSpec(BaseModel):
    """One executable background task declared by an integration manifest.

    `target` is a dotted ref (`"pkg.mod:func"`) resolved by
    `app.plugin.refs.resolve_ref()`. Signature depends on `kind`:

      - `"startup"` — a long-lived task, started once at app boot and run
        until shutdown. Signature: `async def target(stop_event: asyncio.Event)
        -> None`. The kernel supervises it (`app.plugin.supervisor`):
        restarts on unhandled exception with exponential backoff (capped at
        5 min), and returns cleanly once `stop_event` is set.
      - `"cron"` — a periodic job on the shared scheduler, same as a sync
        job. `cron` must be set (a crontab string). Signature: a plain
        `async def target() -> None` (no arguments) — same shape as the
        kernel's own scheduled jobs.
    """

    name: str  # unique job/task id
    target: str  # dotted ref, e.g. "pkg.mod:func"
    kind: Literal["startup", "cron"]
    cron: str | None = None  # required when kind == "cron"
    misfire_grace_time: int = 120  # cron-kind only; APScheduler default here matches pre-3.1
    # Wave 5.11: set False for a cron-kind task whose OWN liveness is already
    # tracked some other way (e.g. a heartbeat probe that writes a `SyncState`
    # row every run) — `app.scheduler._wrap_scheduled_job` then skips the
    # `runs` ledger insert entirely for that job. Measured on
    # `whatsapp_bridge_heartbeat` (every minute): 1,440 identical `ok` rows a
    # day, almost all of `system_alerts`' `recent_runs` axis, none of it
    # information a reader didn't already have from the bridge's own
    # `SyncState` row (which axis 1's `consecutive_failures`/staleness check
    # already reads). Default True — most cron tasks have no other audit
    # trail, and the ledger is the only place "did this run" is answered.
    ledger: bool = True


class IntegrationManifest(BaseModel):
    name: str  # must equal the package name
    display_name: str
    version: str  # start everything at "1.0.0"
    # "deriver" (added with the algo harness) is the type for an integration
    # whose output is *computed* from state comar already holds rather than
    # fetched from anywhere: a solver, a forecaster, a classifier. It is not a
    # cosmetic label — `app.algo.base.AlgoIntegration` is its base class, and
    # the kernel's scoring job (`app.plugin.kernel_jobs`) walks exactly the
    # derivers to grade yesterday's predictions against what actually happened.
    # `commute` predates the type and still declares "source"; converting it is
    # a separate, judged change (its `interchange_delay_min` IS a scoreable
    # prediction, but retrofitting the table is not free).
    type: Literal[
        "source",
        "push_source",
        "bidirectional",
        "action",
        "capability",
        "system",
        "deriver",
    ]
    description: str  # one line
    icon: str  # lucide-react icon component name, e.g. "Calendar" (chunk 1.3)

    # Data ownership
    models: list[str] = []  # ORM class names owned by this integration
    embedding_sources: list[str] = []  # e.g. ["vault"]; [] if none

    # External flows (security-relevant declaration)
    reads_from: list[str] = []  # external systems read, e.g. ["google-calendar-api"]
    writes_to: list[str] = []  # external systems written

    # Scheduling (descriptive for now; consumed in 3.1)
    schedule: str | None = None  # cron string — single source of truth, kernel-scheduled (V4 3.1)
    schedule_timezone: str | None = None
    freshness_threshold_minutes: int | None = None  # mirrors services/freshness.py
    staleness_probe: StalenessProbe | None = None  # mirrors services/data_freshness.py
    background_tasks: list[TaskSpec] = []  # executable — see TaskSpec docstring
    routes: list[str] = []  # dotted refs to APIRouter instances, kernel-mounted

    # Config (V4 chunk 3.3 — replaces the descriptive-only `settings_keys`)
    config_schema: dict[str, ConfigFieldSpec] = {}  # keys read via plugin_config()
    oauth: OAuthRequirement | None = None  # Google OAuth scopes this integration needs

    # Capability contract (V4 chunk 4.2). `provides` names capabilities this
    # integration exposes to others via `app.plugin.capabilities.get_capability()`
    # — by convention, resolved to `app.integrations.<name>.facade:FACADE`.
    # `depends_on` now lists *capability strings* it consumes (e.g.
    # "mail.query"), not integration package names — `app.plugin.validate`
    # checks every entry resolves to some manifest's `provides` list, and that
    # the resulting integration-level dependency graph is acyclic. Capability
    # *enforcement* (who's allowed to call what) is chunk 2.2/on-hold — this
    # is purely a structural/wiring contract for now.
    provides: list[str] = []
    depends_on: list[str] = []
