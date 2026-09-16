# Writing an integration

This is the walkthrough version of the north-star claim from
`vault/Projects/lios/Plans/Shipped/comar-v4/00-index.md`:

> A new integration must be buildable without editing any kernel code —
> drop a package under `server/backend/app/integrations/`, write its
> `manifest.py`, add credentials via config. No edits to `models/__init__.py`,
> `scheduler.py`, `main.py`, `mcp/annotations.py`, `routes/__init__.py`,
> freshness dicts, or the frontend.

`tests/test_drop_in_integration.py` proves this mechanically. This document
proves it in prose, by walking through the scaffold at
`app/integrations/_template/` end to end. Read that package alongside this
file — every section below points at the exact lines that demonstrate it.

## 1. Package layout

```
app/integrations/_template/
├── manifest.py    # MANIFEST: IntegrationManifest — the one file the kernel reads
├── models.py      # SQLAlchemy models this integration owns
├── client.py       # External API wrapper (HTTP calls) — no DB access
├── sync.py         # pull_*/store_* pair — no outbound I/O in store, no DB writes in pull
├── tools.py        # MCP tool definitions, built via the DSL
└── __init__.py     # The BaseIntegration subclass — wires everything above together
```

Five files is the common shape; a few integrations vary it for a good
reason — see `server/CLAUDE.md`'s "Exceptions to the 5-file pattern" (e.g.
`irish_rail` has no `models.py`/`sync.py` because it's a live API with no
cache; `finance` has `services.py` instead of `client.py` because there's no
external API to wrap).

The leading underscore in `_template` isn't cosmetic: both
`app.plugin.discovery.discover_integrations()` and
`app.plugin.validate.discover_manifests()` skip any `app/integrations/*`
directory whose name starts with `_` (`subdir.name.startswith("_")`). That
means `_template` is never imported, registered, scheduled, or validated by
a running server — it exists purely to be copied. To start a real
integration:

1. `cp -r app/integrations/_template app/integrations/<your_name>`
2. Search-and-replace the literal placeholder string
   `__TEMPLATE_INTEGRATION_NAME__` with `<your_name>` across every file in
   the copy — it appears in `manifest.py::MANIFEST.name`, `__init__.py`'s
   `name` property, `sync.py`'s `plugin_config()` call, and `tools.py`'s
   ping payload.
3. Rewrite `manifest.py`, `models.py`, `client.py`, `sync.py`, `tools.py` to
   describe what your integration actually does.
4. Done. No other file in the repo needs to change for the kernel to pick it
   up — that's the whole point.

## 2. Manifest fields

`manifest.py::MANIFEST` (`app.plugin.manifest.IntegrationManifest`) is the
single place the kernel reads to know everything about an integration.
Field-by-field (see `_template/manifest.py`'s inline comments for the exact
same material anchored to a concrete example):

| Field | What it drives |
|---|---|
| `name` | Must equal the package directory name. Checked at boot (`validate_manifests`). |
| `display_name` | Shown in the dashboard. No uniqueness constraint. |
| `version` | Everything starts at `"1.0.0"` (V4 decision 7 — swap = code change + migration, in-repo). |
| `type` | One of `source \| push_source \| bidirectional \| action \| capability \| system` — which typed base class you subclass (section 4 below). |
| `models` | ORM class names this integration owns, by string. Resolved lazily by `app.plugin.discovery.discover_integration_models()` for `app/models/__init__.py` — no import needed there. Checked against the package's actual `models.py` at boot. |
| `embedding_sources` | Source strings fed into the unified `embeddings`/`embedding_queue` tables. Must be globally unique across every manifest. |
| `reads_from` / `writes_to` | Plain-string declaration of external systems touched — not enforced yet, but the one place to grep "what does this talk to". |
| `schedule` / `schedule_timezone` | The ONLY source of truth for cron scheduling (V4 chunk 3.1) — `app/scheduler.py` reads this directly. `None` means "nothing to poll on a schedule". |
| `freshness_threshold_minutes` | Per-tool freshness gate, checked at dispatch time (`app.plugin.dispatch`'s `ensure_fresh`). |
| `staleness_probe` | Dashboard/`system_alerts` staleness check — "is new data actually landing", independent of "did the sync job report success". Usually `model` + `timestamp_column`; two real integrations use `probe_function` instead (see `StalenessProbe`'s docstring in `app/plugin/manifest.py`). |
| `background_tasks` | Executable startup-supervised long-runners or extra cron jobs (`TaskSpec` — see its docstring for `"startup"` vs `"cron"` signatures). |
| `routes` | Dotted refs to `APIRouter` instances the kernel should mount (push ingest routes, OAuth callbacks). |
| `config_schema` | Every config key read via `plugin_config()` (section 5). |
| `oauth` | Google OAuth scopes this integration needs, if any. |
| `provides` / `depends_on` | Capability contract (section 7). |

## 3. Capability naming

A capability string (`provides=["mail.query"]`, `depends_on=["mail.query"]`)
is namespaced `<domain>.<verb>` — `calendar.query`, `sheets.write`,
`whatsapp.query`. Pick a name describing *what the capability does*, not
which integration provides it (the whole point is that the consumer doesn't
need to know or care who the provider is — `app.plugin.capabilities
.get_capability(name)` resolves it). Only declare `provides` when another
integration genuinely needs to call in — don't add a facade speculatively
just because it seems tidy.

## 4. Scoping declaration — `UserOwnedMixin` vs household-shared

Every new table makes an explicit choice (V4 decision 6: "every row has an
owner; 'household' is a declared grant, never the absence of a `user_id`
column"):

- **`UserOwnedMixin`** (`app/mixins.py`) — one NOT NULL `user_id` column
  (FK -> `users.id`, `ON DELETE RESTRICT`, indexed). This is the **safer
  default** for any new table. The DSL builders (`ListTool`/`SearchTool`,
  via `app.tools.helpers.scoped_query`) auto-detect `hasattr(model,
  "user_id")` and inject `WHERE user_id = current_user_id()` for you — no
  hand-written `WHERE` clause to get wrong. `_template/models.py`'s
  `TemplateItem` takes this mixin; its docstring explains the reasoning in
  full, including the composite-uniqueness rule (`UniqueConstraint("user_id",
  "external_id")` — a bare `external_id` constraint would collide across
  users).
- **No mixin at all** — for data that is genuinely household-shared or
  global: `finance` (joint bank accounts), `weather_*` (one household, one
  location), `whatsapp_contacts` (a shared contact graph),
  `historical_documents` (a shared corpus). This has to be a *positive*
  decision, documented in the model's own docstring (see
  `app/mixins.py::UserOwnedMixin`'s own docstring for the canonical list of
  which real tables deliberately opt out and why) — never "I didn't bother
  adding user_id."

When genuinely unsure, add the mixin. Reversing "this table turned out to
need per-user scoping after all" is much more expensive than reversing "this
table turned out to be safely shared" — the latter is a one-line docstring
change plus a migration to drop a column; the former means auditing every
row already written for who actually owns it.

`tests/test_user_scoping.py` is the cross-user leak canary — it sweeps every
`UserOwnedMixin` model through `ListTool`/`SearchTool` and every read-only
tool in the registry, asserting user A never sees user B's rows. A new
integration's tests should include the equivalent check for its own model if
it's user-owned (see the test checklist, section 9).

## 5. Config schema

Old pattern: every integration read its config straight off the global
`HomeSettings` singleton (`settings.lastfm_api_key`). New pattern (V4 chunk
3.3): declare each key in `manifest.py::MANIFEST.config_schema` as a
`ConfigFieldSpec` (`type`, `required`, `secret`, `default`, `description`),
then read it via `app.plugin.config_store.plugin_config(name)`, which
returns a typed pydantic model built from that schema. Values come from the
`integration_config` DB table (secrets Fernet-encrypted at rest), falling
back to a `HOME_<KEY>` environment variable during the transition period
(logged once as a nudge, never an error).

`_template/manifest.py` declares two keys — `api_key` (`required=True,
secret=True`) and `page_size` (`required=False, default=50`) — and
`_template/sync.py::pull_items()` reads them with
`plugin_config("__TEMPLATE_INTEGRATION_NAME__").api_key` /
`.page_size`. `BaseIntegration.is_configured()`'s default implementation
(`app.plugin.config_store.is_configured_from_schema`) is already "True iff
every `required` key resolves to a truthy value" — you only override
`is_configured()` when you need a real connectivity probe beyond "is the key
present" (e.g. `obsidian` checks the vault mount exists on disk;
`google_mail` checks a stored token actually carries the right OAuth scope).

Adding a config key to an existing integration, or a brand-new integration's
whole config surface, never touches `config.py` — one `config_schema` entry
is the whole change. Secrets are masked as `"•••last4"` by the dashboard's
`GET /integrations/{name}/config` route; they're never returned in plaintext
over that API.

## 6. DSL tools + annotations

Every MCP tool a `mcp_tools()` method returns must carry inline
`annotations` — `app.mcp.server.register_mcp_tools()` raises
`MissingAnnotationsError` at startup for any that don't (V4 chunk 1.2 killed
the old centralized `app/mcp/annotations.py` fallback). Build tool dicts via
`app.tools`'s DSL, not hand-assembled dicts:

| Builder | Use for |
|---|---|
| `ListTool` | "List rows in a date range, ordered, limited" — the most common shape. Auto-scopes per-user models, defaults to read-only annotations. |
| `SearchTool` | Multi-column `ILIKE` search with term splitting. |
| `SemanticSearchTool` | pgvector search + an enrich callback. |
| `StatsTool` | Wraps a `compute()` callback returning aggregate stats. |
| `CustomTool` | Wraps an existing hand-written handler that doesn't fit any of the above. **No default annotations** — you must pass them explicitly, because a hand-written handler's read/write/destructive behaviour can't be inferred. |

`_template/tools.py` demonstrates both ends of that spectrum:
`template_list_items` via `ListTool` (schema, auto-scoping, and read-only
annotations all generated for you — you only supply `to_dict`), and
`template_ping` via `CustomTool` (a trivial hand-written handler, with
explicit `ToolAnnotations(read_only_hint=True, idempotent_hint=True)`).

Every real integration that used to hand-assemble a raw `{"inputSchema":
..., "handler": ...}` dict has been converted to one of these builders as of
V4 chunk 4.3 — `git grep -n '"inputSchema"' app/integrations/` should return
nothing except `historical_corpus` (deferred; see that package's own note in
the chunk's Batch progress log).

## 7. Sync contract: pull/store vs push vs action vs capability

`app/plugin/bases.py` has five typed bases, one per manifest `type`. Picking
the right one is the first decision a new integration makes:

| `type` | Base class | Choose when | Real example |
|---|---|---|---|
| `source` | `SourceIntegration` | You poll an external system on a schedule and cache what you get. Write `accounts()`, `pull()`, `store()` — `sync()` is fully inherited (fan-out, cursor bookkeeping, error aggregation all handled for you). | `google_calendar` — multi-account OAuth poll, 15-min cron. `_template` copies this shape. |
| `push_source` | `PushSourceIntegration` | Data arrives via an inbound route (a bridge container POSTing to you, a phone app hitting an ingest endpoint) — there's nothing to poll. No `sync()`; declare ingest route(s) in the manifest and, optionally, a `probe()` liveness check. | `whatsapp` — the Baileys bridge writes directly to Postgres; `probe()` wraps the bridge's HTTP health check. |
| `bidirectional` | `BidirectionalIntegration` | Everything `SourceIntegration` needs, PLUS an outbound write path (create/update against the external system). | `google_calendar` again (its own type is actually `bidirectional`: reads via `SourceIntegration`'s machinery, writes via `create_event`). `apple_reminders` formalizes its existing enqueue -> SSE -> EventKit dispatch path onto `execute_action()`. |
| `action` | `ActionIntegration` | No data to cache at all — every tool call hits the external system directly, nothing to schedule. `sync()`/`dashboard_data()` default to no-ops. | `irish_rail` — live station-departure API, no cache, no cron. |
| `capability` | `CapabilityService` | No external system whatsoever — an in-process tool surface over your own tables, or composing other integrations' facades. `sync()` is a no-op. | `coffee`, `snags` (both: tool surface over their own DB tables, `schedule=None`). `system` (composes other integrations' facades via `get_capability()`, no tables of its own). |

Judgment calls happen at the edges — a couple of real integrations'
`type` field and base class don't perfectly agree (e.g. `attachments` is
typed `"capability"` in its manifest but behaves like a `SourceIntegration`
because it runs a real scheduled scan) — nothing currently *enforces* the
two match, but keeping them aligned is the honest default for anything new.
If your integration's shape doesn't cleanly fit one row above, look at how
`commute` and `homeassistant` handled it (V4 chunk 4.3, Batch D): both kept
`sync()` as an explicit **override** rather than splitting into
`pull()`/`store()`, because their tested sync logic genuinely can't be
cleanly decomposed (a single tightly-coupled function that needs multiple
feeds together to make one decision) — `pull()`/`store()` become unused stub
bodies satisfying the ABC, and that's a legitimate, documented choice, not a
half-finished conversion.

## 8. Background tasks and facades

**Background tasks** (`manifest.py::background_tasks`, a list of
`TaskSpec`): declare a `"startup"` long-runner (supervised, auto-restarted
with backoff — see `homeassistant`'s WS event listener) or a `"cron"` job
beyond the main sync (see `whatsapp`'s bridge-heartbeat, or
`apple_reminders`'s backlog sync). `target` is a dotted ref
(`"pkg.mod:func"`) resolved by `app.plugin.refs.resolve_ref()` — see that
module for the exact signature each `kind` expects.

**Cross-integration calls** (facades, V4 chunk 4.2): an integration may
*never* `import app.integrations.<other>.tools` / `.models` / `.client` /
etc. directly — `tests/test_capability_boundaries.py` enforces this by
walking every `.py` file under `app/integrations/`. If your integration
needs another one's behavior:

1. The **providing** integration exposes a small facade class in its own
   `app/integrations/<name>/facade.py`, with a module-level singleton
   `FACADE = XFacade()`, and declares the capability name(s) in its
   manifest's `provides`.
2. The **consuming** integration declares the capability string(s) it needs
   in its own manifest's `depends_on` — `app.plugin.validate` fails boot if
   an entry doesn't resolve to any manifest's `provides`, or if it would
   create a dependency cycle.
3. At call time: `app.plugin.capabilities.get_capability("mail.query")`
   (returns the provider's `FACADE`), or — for a fixed 1:1 dependency where
   the indirection buys nothing — import the facade module directly
   (`from app.integrations.google_mail.facade import FACADE`). Either way,
   `<pkg>.facade` is the only cross-package import surface.

**Never `from module import fn` inside a facade or any wiring code** — that
copies the reference at import time, silently breaking tests that
`monkeypatch.setattr("app.integrations.x.module.fn", ...)` (the standard
pattern in this repo). Always `from app.integrations.x import module as
_module`, then call `_module.fn(...)` at call time. See
`google_mail/facade.py`, `media/facade.py`, `sheets/facade.py` for the
canonical shape — this was learned the hard way (see the "Hard rules learned
in execution" section of `vault/Projects/lios/Plans/Shipped/comar-v4/00-index.md`).

**Optional: `has_data(session, user_id) -> bool` on your facade.**
sam-rollout D1 (`app.mcp.instructions.render_instructions_for_user`) calls
this — if present — to decide whether to offer your integration in a given
user's personalized "Your Setup" instructions section, so a user with no
Last.fm scrobbles or no coffee log isn't told about tools they'll never use.
Only worth adding for integrations with genuinely private, sometimes-empty
per-user data (see `lastfm/facade.py`, `apple_health/facade.py`,
`google_mail/facade.py`, `coffee/facade.py`, `whatsapp/facade.py` for the
existing cheap-`COUNT`-query shape) — a household-shared or always-on
integration doesn't need one. Nothing calls this automatically; skipping it
just means the integration is never called out by name in the personalized
instructions, which is a fine default.

**Enable/disable is orthogonal to configuration and needs no per-integration
code.** Every integration gets a kernel-level on/off switch for free
(`integration_config`'s reserved `__enabled__` key, V4 chunk 5.1,
`GET/PUT /api/integrations/{name}/enabled`) — defaults to enabled, gates
both MCP tool registration and scheduler jobs at the next process start.
Nothing in a new integration's own code needs to check or implement this.

## 9. Test checklist for a new integration

At minimum, write:

- **Sync contract**: `integration.sync` is a plain `def`, not a coroutine
  (`tests/test_sync_contract.py` already sweeps every registered
  integration for this — a new one is covered automatically once
  registered, but write a focused unit test for your `pull()`/`store()`
  functions directly too).
- **Manifest validity**: exercised automatically by
  `tests/test_manifests.py`'s real-tree tests once your integration is
  registered (not underscore-prefixed) — no new test needed there, but run
  it locally after wiring your package in to catch a typo before it fails
  someone else's CI.
- **User scoping** (if your model takes `UserOwnedMixin`): a focused test
  that user A's tool calls never return user B's rows — the same shape as
  the per-model cases already in `tests/test_user_scoping.py`.
- **Tool annotations**: every tool dict has `annotations` set (this fails
  loudly at server startup if missed, but a fast unit test catches it before
  that).
- **Handler behavior**: at least one test per tool handler, covering the
  happy path and one error/empty-result path.
- **Config-gated `is_configured()`**: if you override the default, test
  both the configured and unconfigured cases.
- **Facade boundary** (if you declare `provides`): a test that another
  package's capability lookup actually resolves to your `FACADE`.
- **No behavior-change regressions**: if you're converting an existing
  hand-rolled integration onto a typed base (rather than writing a new one),
  run `tests/test_tool_snapshots.py` before/after if your integration is in
  its scope — it must stay byte-identical.

See `tests/test_drop_in_integration.py` for the project's own acceptance
proof that this whole chapter is true end-to-end: it copies `_template`
under a throwaway name, boots discovery, and asserts the new integration is
fully live — in the registry, its models in metadata, its tools registered
with annotations, its schedule in the job set, its config schema served —
with zero kernel edits. It also proves the negative: each of a few
deliberately-broken variants (missing manifest field, an undeclared model,
an unresolvable capability dependency) fails startup validation loudly,
rather than silently misregistering.
