# comar-server

Data engine for the comar (Co-Managed Archive) family knowledge system. Caches data from external APIs in Postgres, exposes tools over MCP (Streamable HTTP at `/mcp/`) and a smaller HTTP+SSE control plane (`/api/v1/*`) for the local daemon, and serves a web dashboard. Docker-deployed to a home server.

## Tech Stack

- **Backend**: Python 3.12, FastAPI, coglib (shared config + DB + logging + LLM calls, `lios/libs/coglib`), SQLAlchemy 2.0, Alembic (schema migrations), Pydantic 2.0
- **Frontend**: React 19, Vite 6, Tailwind v4, React Router 7, TypeScript 5
- **UI**: Components from cog-ui (alexunism), dark-mode-first theme
- **Database**: Postgres 16 with pgvector extension (for embedding search)
- **Embeddings**: fastembed (BAAI/bge-small-en-v1.5, 384-dim) — unified pipeline (vault, Gmail, WhatsApp)
- **WhatsApp Bridge**: Node.js sidecar container (Baileys @whiskeysockets/baileys), writes to shared Postgres
- **HTTP API (V3)**: `/api/v1/*` on FastAPI port 8400 — tools, push (vault/reminders/health/logs), events (SSE), heartbeat, prompts, instructions. Bearer-token auth via `client_tokens`. **No gRPC, no protobuf.**
- **MCP**: Python `mcp` SDK; **stateless Streamable HTTP** transport at `/mcp/`, per-user bearer auth. Claude Code (and any other MCP client) connects directly — this is the only tool path. The local daemon is a pure side-car (EventKit + vault watcher); its legacy localhost MCP proxy was retired in Phase 4 (2026-07-14, client 2.4.0) and its local port serves `GET /health` only.
- **Scheduling**: APScheduler (AsyncIOScheduler) for background sync jobs
- **Auth**: Bearer token cookie for web UI (`HOME_UI_TOKEN`), per-user bearer for MCP + HTTP API (`client_tokens` table — hashed at rest, with expiry; the old shared-secret `HOME_MCP_TOKEN` admin fallback was removed in V4 chunk 2.3), Google OAuth for calendar/Gmail

## Architecture

```
Claude Code (and any other MCP client)
  ↕ MCP / Streamable HTTP (per-user bearer, port 8400)
comar-server container (SERVER_IP)
  ├── FastAPI app (port 8000 internally → host 8400)
  │   ├── /mcp/         (MCP, stateless Streamable HTTP — primary tool surface)
  │   ├── /api/v1/*     (control plane for the daemon: heartbeat, vault/push,
  │   │                  reminders/push, health/push, logs/push, events SSE,
  │   │                  prompts, instructions — bearer-token auth)
  │   ├── /api/* routes (dashboard, integrations, auth, client dist — UI cookie auth)
  │   └── /* (static frontend from Vite build)

lios-sync (on each Mac, pure side-car — tool path is server /mcp/;
              local port 9400 serves GET /health only, proxy retired Phase 4)
  ↕ HTTPS / SSE callback (per-user bearer)
  ├── Vault watcher: fsevents → POST /api/v1/vault/push
  └── Reminders: subscribes to server SSE callbacks → executes EventKit (PyObjC)
  ├── Vault watcher (watchdog on /obsidian, debounced re-indexing)
  ├── Caddy reverse proxy (from infra project) — https://comar.lab
  ├── APScheduler (background sync jobs per integration)
  ├── Embedding worker (5 min cycle, processes unified queue)
  └── Postgres 16 + pgvector (all cached data + embeddings)

whatsapp-bridge container (comar-whatsapp)
  ├── Baileys (WhatsApp Web multi-device protocol)
  ├── Read-only (sendMessage, sendReadReceipt, presence all stubbed)
  ├── Writes to whatsapp_messages + whatsapp_contacts in shared Postgres
  ├── History sync (SYNC_FULL_HISTORY env var, one-time backfill)
  └── Health endpoint (GET http://localhost:3100/health)
```

## Project Structure

```
server/
├── CLAUDE.md                  # This file
├── Makefile                   # Dev and deploy commands
├── Dockerfile                 # Python 3.12-slim, coglib, fastembed
├── docker-compose.yml         # Postgres (pgvector:pg16) + FastAPI app
├── .env                       # Secrets (not in git)
├── csv-inbox/                 # Drop CSVs here for /import-finance
│   └── archive/               # Processed CSVs moved here
├── certs/                     # TLS certs (not in git)
├── .libs/                     # gitignored; `make stage-libs` copies lios/libs/coglib here for the Docker build
│
├── client-dist/               # Built client wheels (served by /api/client/)
├── backend/
│   ├── requirements.txt
│   ├── alembic.ini            # Alembic config (DB URL overridden by HOME_DATABASE__URL)
│   ├── alembic/               # Schema migrations
│   │   ├── env.py             # Reads HOME_DATABASE__URL, imports all models
│   │   └── versions/          # Migration scripts (date-prefixed filenames)
│   ├── requirements-dev.txt   # Test-only deps (pytest, testcontainers) — not in the image
│   ├── tests/                 # Two tiers (run via `make test-server`)
│   │   │   # `unit` (default marker): mocked DB, <1s — select with `-m "not db"`
│   │   │   # `db`: real pgvector Postgres — testcontainers locally (needs
│   │   │   #   Docker; skips loudly without it) or COMAR_TEST_DATABASE_URL in CI
│   │   ├── conftest.py        # Markers, mock_session, pg container fixture,
│   │   │                      #   real_db (points get_db() at the container,
│   │   │                      #   truncate-isolation), db_session
│   │   ├── test_db_harness.py        # Schema-at-head, ORM/migration no-drift,
│   │   │                             #   Phase-0 migration round-trip
│   │   ├── test_user_scoping.py      # Enforcement: every UserOwnedMixin model
│   │   │                             #   through ListTool/SearchTool + leak-canary
│   │   │                             #   sweep over all read-only tools + meta-test
│   │   ├── test_embedding_pipeline.py # Queue, poison bisection, pgvector search scoping
│   │   ├── test_mcp_transport.py     # ASGI /mcp: 3 auth paths, dispatch, timeout, 410
│   │   ├── test_scheduler.py         # run_sync state machine on real SyncState
│   │   │                             #   incl. Transient/Permanent/NeedsReauth paths
│   │   ├── test_sync_contract.py     # Every integration's sync() must be plain def
│   │   ├── test_tool_snapshots.py    # Golden-output suite (51 snapshots, 11 integrations
│   │   │                             #   in scope: lastfm, obsidian, whatsapp, google_mail,
│   │   │                             #   coffee, finance, apple_health, media, attachments,
│   │   │                             #   homeassistant, snags); goldens in tests/snapshots/
│   │   │                             #   (gitignore-negated), regenerate ONLY deliberately
│   │   │                             #   via UPDATE_SNAPSHOTS=1
│   │   ├── test_tool_calls.py        # tool_calls persistence + system_alerts tool checks
│   │   ├── test_drop_in_integration.py # North-star proof (V4 chunk 4.3e): copies
│   │   │                             #   `_template` under a throwaway name, boots
│   │   │                             #   discovery, asserts it's fully live (registry,
│   │   │                             #   models, tools+annotations, schedule, config
│   │   │                             #   schema) with zero kernel edits — plus deliberately
│   │   │                             #   broken variants fail validation loudly
│   │   ├── test_algo_harness.py       # The algo harness end to end: a synthetic deriver
│   │   │                             #   driven through train → JSON artifact → predict →
│   │   │                             #   record → score → positive skill vs persistence,
│   │   │                             #   plus the deriver drop-in (zero kernel edits)
│   │   ├── test_kernel_import_guard.py # Kernel (app/plugin, app/mcp, app/models,
│   │   │                             #   scheduler.py, main.py, app/routes, app/services)
│   │   │                             #   may not import a specific integration's internals
│   │   │                             #   except via the registry facade, `<pkg>.facade`, or
│   │   │                             #   dynamic-string imports — small documented allowlist
│   │   └── ...                       # unit-tier files (lastfm, auth, encryption, …)
│   └── app/
│       ├── main.py            # FastAPI app, lifespan, auth middleware
│       ├── config.py          # HomeSettings(CogSettings), HOME_ prefix
│       ├── db.py              # Postgres singleton + pgvector init + Alembic migrations + SessionDep
│       ├── mixins.py          # SourcedRecordMixin (source_id, source_ts, synced_at, content_hash)
│       ├── errors.py          # ComarError → TransientError (retried) / PermanentError
│       │                      #   (no-retry); NeedsReauthError(PermanentError), re-exported
│       │                      #   from auth/oauth for back-compat
│       ├── scheduler.py       # APScheduler: per-integration cron, timeout, sync state;
│       │                      #   classifies failures by isinstance on app.errors classes
│       ├── auth/
│       │   └── oauth.py       # Google OAuth: auth URL, token exchange, credential refresh
│       ├── models/
│       │   ├── __init__.py    # *** MUST import all models for create_tables() ***
│       │   ├── tokens.py      # OAuthToken, SyncState
│       │   ├── clients.py     # ClientToken (HTTP/MCP device auth), ClientLog (remote log shipping)
│       │   └── algo.py        # AlgoPrediction / AlgoModelVersion / AlgoRun — shared by every
│       │                      #   deriver; kernel-owned so adding one stays zero-edit
│       ├── routes/
│       │   ├── __init__.py    # /api prefix, mounts all routers
│       │   ├── auth.py        # Login, check, Google OAuth callback, token list
│       │   ├── dashboard.py   # GET /api/dashboard/summary (aggregates all integrations)
│       │   ├── integrations.py # List, sync trigger, backfill endpoints
│       │   └── client_dist.py # Client wheel download, version check, bootstrap script
│       ├── mcp/
│       │   └── server.py      # MCP server (Streamable HTTP), auto-discovers tools, asyncio.to_thread
│       ├── algo/              # AI/predictive harness — peer of tools/, imported by
│       │                      #   `type="deriver"` integrations. spec/features/estimators/
│       │                      #   artifacts/predictions/scoring/sinks/llm/base.
│       │                      #   See server/docs/writing-a-deriver.md
│       ├── tools/              # Declarative tool DSL (V2)
│       │   ├── __init__.py    # Re-exports: ListTool, SearchTool, SemanticSearchTool, StatsTool, CustomTool
│       │   ├── base.py        # ToolBuilder ABC, ExtraFilter dataclass, CustomTool, parse_iso_date
│       │   ├── list_tool.py   # ListTool: date range + filters + ordering + formatting
│       │   ├── search_tool.py # SearchTool: multi-column ILIKE + term splitting
│       │   ├── semantic_tool.py # SemanticSearchTool: pgvector + enrich callback
│       │   ├── stats_tool.py  # StatsTool: compute callback wrapper
│       │   └── helpers.py     # Shared handler primitives: scoped_query (THE one
│       │                      #   user-scoping impl — DSL routes through it too),
│       │                      #   serialize, make_enrich, period_since, top_n_group_by.
│       │                      #   New integrations use these, don't copy-paste _scoped/_to_dict
│       ├── stream_manager.py   # Pub/sub hub for server-push events (SSE + WebSocket)
│       ├── api/                # V3 client API
│       │   └── v1.py           # Tools, push, events, heartbeat, instructions
│       ├── auth/
│       │   └── client_token.py # FastAPI dependency: validate bearer → user
│       ├── mcp/
│       │   ├── server.py       # MCP server (Streamable HTTP), tool registration
│       │   ├── instructions.py # Canonical preamble (single source of truth)
│       │   └── annotations.py  # Centralized read/write/destructive hints per tool
│       ├── services/
│       │   ├── embedding.py   # Unified EmbeddingService: enqueue, search, worker (fastembed + pgvector)
│       │   └── freshness.py   # Data freshness checks for dashboard
│       └── integrations/      # All integration packages (see below)
│
├── frontend/
│   ├── package.json           # React 19, Vite 6, Tailwind v4
│   ├── vite.config.ts         # Proxy /api → localhost:8000 (dev)
│   └── src/
│       ├── main.tsx           # React entry point
│       ├── App.tsx            # Router, sidebar, auth gate
│       ├── index.css          # Dark theme CSS variables
│       ├── pages/
│       │   ├── Dashboard.tsx  # 4-panel grid: calendar, reminders, finance, status
│       │   ├── Integrations.tsx # Integration status, manual sync buttons
│       │   ├── Settings.tsx   # Google account management, token status
│       │   └── Login.tsx      # Token entry screen
│       ├── components/
│       │   ├── layout/        # app-layout, sidebar-nav, bottom-nav, page-header
│       │   └── ui/            # 18 components from cog-ui (button, card, stat-card, etc.)
│       ├── lib/
│       │   ├── api.ts         # Typed API client with auth dispatch
│       │   └── utils.ts       # cn() helper (clsx + tailwind-merge)
│       └── hooks/
│
├── whatsapp-bridge/           # Node.js WhatsApp sidecar container
│   ├── Dockerfile             # node:22-slim, HTTPS git rewrite for Baileys
│   ├── package.json           # Baileys, pg, pino, qrcode-terminal
│   └── src/
│       ├── index.js           # Baileys socket, QR pairing, history sync, health endpoint
│       └── db.js              # Postgres upsert for messages + contacts (batch + single)
│
└── scripts/                   # (the standalone reminders-sync/ Mac agent was
                                #  removed 2026-08-08 — dead code, superseded
                                #  by the client daemon's EventKit path)
```

## Integration Pattern

Every integration lives in `backend/app/integrations/<name>/`, declares a `manifest.py` (`IntegrationManifest` — name, type, models, schedule, config_schema, capabilities), and subclasses one of the typed bases in `app/plugin/bases.py` (`SourceIntegration` / `PushSourceIntegration` / `BidirectionalIntegration` / `ActionIntegration` / `CapabilityService`), all of which subclass the original `BaseIntegration` ABC. The common file shape:

| File | Purpose |
|------|---------|
| `manifest.py` | `MANIFEST: IntegrationManifest` — the one file the kernel reads (models owned, schedule, config keys, capabilities) |
| `__init__.py` | Class implementing the typed base — writes only the handful of methods specific to it (`accounts()`/`pull()`/`store()` for a `source`, tool handlers only for an `action`, etc.) — `sync()`, fan-out, cursor bookkeeping are inherited |
| `client.py` | External API access (HTTP calls, SDK wrappers) |
| `models.py` | SQLAlchemy models (inherit `coglib.Base`) |
| `sync.py` | `pull_*`/`store_*` pair (data sync logic, called by scheduler or manually) |
| `tools.py` | MCP tool definitions built via the DSL (`ListTool`/`SearchTool`/`SemanticSearchTool`/`StatsTool`/`CustomTool`) — zero raw `{"inputSchema": ...}` dicts left anywhere under `app/integrations/` |

**Exceptions to the 5-file pattern:**
- `apple_reminders/` also has `commands.py` + `routes.py` — this is the LIVE server-enqueue → SSE → EventKit dispatch path (used by tools.py, api/v1.py, backlog_sync.py), not dead code — and `backlog_sync.py` (vault↔Reminders two-way sync via Haiku)
- `finance/` has `services.py` instead of `client.py` (no external API — CSV parsing, categorisation, analytics, transfer detection)
- `irish_rail/` has no `models.py` or `sync.py` (live API, no caching)
- `whatsapp/` has no `client.py` (bridge writes directly to DB; sync.py handles conversation-window chunking for embeddings)
- `system/` has only `__init__.py` + `tools.py` (no client, models, or sync — diagnostic tools that query cross-integration state)
- Facade-exposing integrations add `facade.py` (module-level `FACADE` singleton — the only cross-package import surface; see below)

### Adding an Integration

North star (V4 chunks 4.1–5.1, complete): a new integration is a package + manifest + config — **zero kernel edits**. Discovery (`app.plugin.discovery`) walks `app/integrations/*` on disk, imports each package's `manifest.py` and `__init__.py`, and registers whatever `BaseIntegration` subclass it finds — no hand-maintained import lists anywhere.

1. `cp -r app/integrations/_template app/integrations/<name>` and follow `server/docs/writing-an-integration.md` end to end (it walks the scaffold section by section: manifest fields, config schema, DSL tools + annotations, sync contract by `type`, facades, test checklist).
2. Models are discovered from the manifest's `models: list[str]` (`app.plugin.discovery.discover_integration_models()`) — nothing to add to `app/models/__init__.py`.
3. Config keys go in `manifest.py::MANIFEST.config_schema`, read via `app.plugin.config_store.plugin_config(name)` — nothing to add to `HomeSettings`/`config.py`.
4. Scheduling, MCP tool registration (gated on `enabled` + `is_configured()`), and freshness checks all come from the manifest.
5. `tests/test_drop_in_integration.py` is the project's own acceptance proof of this claim — it copies `_template` under a throwaway name and asserts it's fully live with zero kernel edits, plus that deliberately-broken variants fail validation loudly. `tests/test_kernel_import_guard.py` enforces the flip side: kernel code (`app/plugin`, `app/mcp`, `app/models`, `app/scheduler.py`, `app/main.py`, `app/routes`, `app/services`) may not import a specific integration's internals except via the registry facade, `<pkg>.facade`, or dynamic-string imports (small, documented allowlist of 2 for pre-existing exceptions).

### Writing an integration (typed bases — V4 chunk 4.1)

`app/plugin/bases.py` adds typed subclasses of `BaseIntegration` matching the
manifest `type` field, so a new integration only writes the handful of
methods actually specific to it instead of re-implementing fan-out, error
classification, and cursor bookkeeping every time:

| Manifest `type` | Base class | Write |
|---|---|---|
| `source` | `SourceIntegration` | `accounts()`, `pull()`, `store()` — `sync()` is inherited |
| `push_source` | `PushSourceIntegration` | ingest route(s) + optional `probe()` liveness check; no `sync()` |
| `bidirectional` | `BidirectionalIntegration` | everything `SourceIntegration` needs, plus (later) `execute_action()` |
| `action` | `ActionIntegration` | tool handlers only — no pull, no cached table |
| `capability` | `CapabilityService` | no external system — serves tools/other plugins from in-process state |
| `deriver` | `AlgoIntegration` (`app/algo/`) | `features()` + `observe()` — output computed from state comar already holds. Prediction cycle, training loop, artifact versioning, HA sensor, two MCP tools and scoring are all inherited |

**`google_calendar` is the reference conversion — copy its shape for any new
polling/bidirectional integration.** `client.py` is the external API wrapper
(HTTP-error classification delegates to `app.plugin.sync_runtime.classify_exc`,
never a local copy); `sync.py` is a `pull_*`/`store_*` pair, not one big
sync function; `__init__.py` is ~30 lines wiring manifest + `accounts()` +
`pull()`/`store()` + tools + dashboard data — `sync()` itself is entirely
inherited; `tools.py` builds tool dicts via the DSL (`CustomTool` here, since
its handlers don't fit `ListTool`'s after/before-date shape) with every tool
still carrying its own inline `annotations`. The full scaffold/checklist doc
for a from-scratch new integration is `server/docs/writing-an-integration.md`,
walking through the `app/integrations/_template/` scaffold end to end
(V4 chunk 4.3e) — start there; `google_calendar/` remains the best real-code
reference for a polling/bidirectional shape.

### Cross-plugin dependencies: capabilities + facades (V4 chunk 4.2)

If your integration needs to call another integration's behavior, it may
**never** `import app.integrations.<other>.tools` / `.models` / `.client` /
etc. directly — that's package internals. Instead:

1. The providing integration exposes a small facade class in its own
   `app/integrations/<name>/facade.py`, with a module-level singleton
   `FACADE = XFacade()`, and declares the capability name(s) it exposes in
   its manifest's `provides: list[str]` (e.g. `provides=["mail.query"]`).
2. The consuming integration declares the capability string(s) it needs in
   its own manifest's `depends_on` (e.g. `depends_on=["mail.query"]`) —
   `app/plugin/validate.py` fails boot if a `depends_on` entry doesn't
   resolve to any manifest's `provides`, and if two manifests claim the same
   capability.
3. At call time, resolve it via `app.plugin.capabilities.get_capability("mail.query")`
   (returns the provider's `FACADE`), or — for a fixed 1:1 dependency where
   the indirection buys nothing — import the facade module directly
   (`from app.integrations.google_mail.facade import FACADE`). Both are
   fine; `<pkg>.facade` is the only cross-package import surface either way
   (`tests/test_capability_boundaries.py` enforces this by walking every
   `.py` file under `app/integrations/`).

Capability *enforcement* (who's allowed to call what) is chunk 2.2,
deliberately on hold pending sam-rollout Phase B-D — today's `provides`/
`depends_on` is a structural wiring contract, not an authz boundary.
`sheets`' credential delegation (which Google account's OAuth token a given
export call is authorized to use) is chunk 2.4's job, same reason.

### The algo harness — AI/predictive work (`app/algo/`)

A shared environment for the algorithmic and predictive layer: the commute
solver's successors, forecasters, LLM-judgement passes. Kernel infrastructure
and a peer of `app/tools/` — integrations import it; it is not an integration.
`type="deriver"` selects it. Full walkthrough: `server/docs/writing-a-deriver.md`.

**Why it exists.** `hardware/homeassistant/commute/` and
`app/integrations/commute/` are the same solver, forked — one ran as a pyscript
shim inside HA, this one runs here, and the HA copy went stale (2026-07-16, 139
lines against this one's 398) with nothing complaining. Every algorithmic thing
needs the same five parts and none of them are the interesting part of an algo:

| | module | the rule |
|---|---|---|
| input | `features.py` | **one** `features()`, called by both training and serving — train/serve skew has no seam to open in |
| models | `estimators.py`, `artifacts.py` | fitted params as JSON (never a pickle), versioned, activated deliberately |
| output | `predictions.py`, `sinks.py` | Postgres rows + an HA sensor + two generated MCP tools, from one `AlgoSpec` |
| judgement | `llm.py` | `coglib.llm`, tokens + cost onto the `AlgoRun` row |
| proof | `scoring.py` | graded against reality; skill vs a baseline recorded at prediction time |

**Three kernel-owned tables**, not per-integration ones: `algo_predictions`
(`made_at` **and** `target_at` — a predictor's output is only verifiable later),
`algo_model_versions` (JSONB params, `is_active`), `algo_runs` (the deriver
equivalent of `SyncState`, plus LLM cost). Kernel-owned because the manifest
requires an integration's models to resolve in its own `models.py`, so
per-integration ownership would mean one predictions table per algo and
per-algo scoring code. **Adding a deriver is still zero kernel edits** —
`tests/test_algo_harness.py::TestDeriverDropsIn` proves it.

**Three cadences:** `MANIFEST.schedule` → `sync()` → `run_predict()` (often);
`MANIFEST.background_tasks` → training (rarely — refitting per cycle makes every
prediction unreproducible and lets one bad week replace a working model);
kernel job `score_algo_predictions` at `12 * * * *` (automatic, for every
deriver — a per-deriver scoring cron fails silently, and a silent scoring
outage looks exactly like a working forecaster).

**Traps worth knowing before writing one:**
- `features()` is called with a *historical* `made_at` during training, so it
  may only read tables that keep history. `ha_entities` holds the latest state
  only — reading it from a feature builder makes every training row see today's
  value while claiming to be last March, and the model scores beautifully and
  predicts nothing. Use `homeassistant.entities`' `numeric_history()`.
- ⚠️ That reads `ha_state_changes`, which records numeric→numeric transitions
  **only when `ha_record_numeric_history` is true** — default false. A fresh
  deployment therefore has no numeric history at all and `train()` correctly
  reports `insufficient_rows`.
- `observe()` returns `None`, never `0.0`, for "not observable yet" — a zero is
  an error the full size of the prediction. Unobservable rows are written off
  after 7 days (`scored_at` set, `actual` NULL) rather than retried forever.
- Serving imports numpy only; scikit-learn is a *fit-time* dependency. A model
  that can't be serialised to JSON doesn't ship — this repo already caps every
  dep's major because an unpinned bump broke the tool surface once, and a
  pickled estimator turns that into a silently wrong forecast.
- An entity_id in a committed `AlgoSpec` is what `test_personalisation_guard`
  sweeps for; resolve it via `ha_entity_for()` from config.

**Status.** Measured 2026-08-29 by instantiating the registry: **28
integrations, 134 tools, 1 registered deriver** (`solar_forecast`).
`_algo_template/` is the scaffold, `tests/test_algo_harness.py` is the
verification (a synthetic deriver driven through train → predict → score →
positive skill). `commute` predates the type and still declares `source` — its
`interchange_delay_min` is a genuinely scoreable prediction, but retrofitting
the table is a separate, judged change.

⚠️ **`solar_forecast` is deployed-but-idle until it is configured**, and its
silence is correct rather than broken. Setup order is in
`app/integrations/solar_forecast/README.md`: allowlist the four entities for
numeric history, run `ha_backfill_history` to import HA's own recorder, then
configure and train. Until then `train()` reports `insufficient_rows` and
`run_predict()` is a clean no-op — there is no history to fit on, because comar
records numeric history for nothing by default.

**Two pieces of plumbing the first deriver needed**, both in `homeassistant`:
- **`ha_numeric_history_entities`** — a per-entity allowlist for
  numeric→numeric transitions. A deriver's `features()` is called with a
  *historical* timestamp during training, so it can only be trained on entities
  whose history is actually kept; the pre-existing `ha_record_numeric_history`
  is a global firehose over ~1,700 entities and the wrong tool for four sensors.
- **`ha_backfill_history`** (tool) + `homeassistant/backfill.py` — imports HA's
  recorder history for the allowlist. Without it a newly-allowlisted entity has
  no past, so a new forecaster is blind for its whole training window. Bounded
  by HA's `purge_keep_days` (default 10) and idempotent on
  `(entity_id, changed_at)`.

**And one bug the first deriver flushed out of the harness:** `training_pairs()`
built its grid from the clock rather than from the stride, so an hourly source
whose samples land on the hour was missed entirely by a grid offset to :27 — and
the only symptom was `insufficient_rows`, a forecaster that silently never
trains. Fixed (`_floor_to_stride`) and pinned by a test.

### Current Integrations (28 packages, 134 tools — measured 2026-08-29 by instantiating the registry; `sheets`, `transcription` and `vision` are pure facades registering no tools of their own)

⚠️ This heading said **25** while the tree held 26, before `solar_forecast` made it 27 — the same drift the root `CLAUDE.md` warns about for the tool count. Measure it (`sum(len(i.mcp_tools()) for i in get_all().values())`), don't increment it.

| Integration | Sync Schedule | MCP Tools | Data Source | Notes |
|-------------|--------------|-----------|-------------|-------|
| `google_calendar` | */15 * * * * | 4 | Google Calendar API (OAuth, read/write) | Multi-account (4+ personal + work), `create_event` tool. Reference typed-base conversion (`bidirectional`) — copy its shape for any new integration. |
| `google_docs` | None (every call is user- or caller-initiated) | 5 | Google Docs API + Drive API (OAuth, read/write) | **Added 2026-08-20.** The companion to `sheets`, same create-once/overwrite-on-write contract (`doc_exports` keyed by `key`, same document id forever so the URL and shares survive a rewrite), but an `ActionIntegration` with real tools rather than a tool-less facade — reading and editing a document is something a person asks for directly. Tools: `docs_read` / `docs_write` / `docs_append` / `docs_replace` / `docs_list`. `provides=["docs.write"]`. 🔑 **Split across two Google APIs on purpose.** Whole-document writes upload HTML through Drive with `mimeType: application/vnd.google-apps.document` and let Google convert it, because the Docs API's `batchUpdate` is *index-based* — every insertion shifts every later offset, so building headings and tables that way is where all the effort would go. Reads and targeted edits use the Docs API, where the offsets are either absent (`replaceAllText`) or trivial (one `insertText` at the end index). `markup.py` holds both converters, hand-written because no markdown library is in `requirements.txt`. ⚠️ **The two halves reach different documents.** `drive.file` is a *per-file* grant, so whole-document overwrite works only on documents comar created; `documents` is account-wide, so read/append/replace work on anything the account can open, including hand-made docs. Widening to `.../auth/drive` would remove the asymmetry at the cost of full Drive access for every integration sharing the scope union — not taken. ⚠️ `docs_append` inserts **literal text**: `## Heading` appends those characters, it does not create a heading (that needs a second `updateParagraphStyle` over the range the insert created). Use `docs_write` for formatted content. ⚠️ The `documents` scope is **new to the OAuth union**, so tokens minted before 2026-08-20 do not carry it — re-consent is required before any tool here works (a 403 that classifies as `PermanentError`, not a retry). `docs_owner_account` is deliberately **not** `required` config, so the read paths keep working unconfigured; the calls that need it raise a `PermanentError` naming the key. Both config keys are prefixed `docs_` because `plugin_config` derives the env fallback as `HOME_<KEY>` — a bare `owner_account` would have claimed the global name `HOME_OWNER_ACCOUNT`. 🔑 **Which account's token is used depends on the operation, not on config alone.** Creating a document prefers `docs_owner_account` so a shared doc does not change hands depending on who asked; reading or editing an existing one prefers the **caller's** own token, because the document is usually theirs and the owner account may not be able to see it at all. Preferring the owner on reads would fail on exactly the documents a person is most likely to ask about. Each is the other's fallback, so a one-Google-account household sees no difference. |
| `google_mail` | */15 * * * * | 8 | Gmail API (OAuth) | 9,961+ messages, pgvector semantic search |
| `apple_reminders` | Push (EventKit, 30s) + reactive backlog sync | 6 | EventKit via PyObjC on client | `reminders_add`/`complete` on client. Reactive vault sync on changes, now per-user (loops active users, own vault path — sam-rollout A3). |
| `apple_health` | Push (no scheduled sync) | 7 | Health Auto Export iOS app → `/api/v1/health/push` (v3) or `/api/health/push` (legacy) | Daily metrics + workouts + sleep. SyncState bumped on push for freshness alerts. Legacy push route now resolves the caller from a per-user `client_tokens` bearer (sam-rollout A2) instead of defaulting to user_id=1. **The phone must point at the tailnet URL, not the LAN IP** — one URL is all the iOS app supports, and it has to resolve off-network. Configure a *trailing* 7-day export window rather than an incremental cursor: pushes made while Tailscale is down are lost, and the upsert keys make re-sending free, so the next success repairs the hole. **Three alerting axes, because there are three different questions** (2026-08-23): the manifest probe asks *how old is the newest row* (36h), `facade.coverage_gaps()` asks *is any day missing*, and `facade.push_silence()` asks *is the phone still calling at all* (12h, read off `SyncState.last_sync_at`). The third was added after a measured incident: pushes stopped 2026-08-22 09:00 and `system_alerts` still said `status: "ok"` 37 hours later, because the first two both reason about data comar *received* and neither had expired. ⚠️ Its 12h threshold is **provisional** — nothing recorded push *attempts* before this change, so there was no cadence to derive it from; `SyncHistory` now accumulates them, so measure before trusting it. Related: **every exit from the push route now writes SyncState** (`routes._record_push`) — previously only the success path did, so `consecutive_failures` could never leave 0 and a rejected payload was indistinguishable from a silent phone. A partial parse records `status="ok"` *with* a `last_error` string rather than a failure status, since the sync did succeed and marking it failing would send someone to debug an outage that isn't happening. **The parser no longer 422s a whole export for one bad record** — critical here specifically because the trailing window re-sends that same record, so a transient-looking fault was actually permanent. Longer gaps are caught by `facade.coverage_gaps()` (capability `health.coverage`), a `system`-alerts axis that looks for holes in the `date` column — the manifest staleness probe can't, since it reads MAX(`synced_at`) which a re-send keeps green, and does so table-wide so one working phone masks another's dead one. |
| `strava` | */30 * * * * | 4 | Strava API v3 (OAuth2, read-only) | **Added 2026-08-29.** Activity archive — runs, rides, walks, swims, gym sessions with distance, pace, elevation, HR and power. Own `strava_activities` table rather than rows in `health_workouts`: an integration owns its models, and Strava carries fields Apple Health has no concept of (power, gear, route polyline). The two coexist; nothing deduplicates across them. 🔑 **Its OAuth is its own.** `manifest.oauth` is the *Google* scope union that `app/auth/oauth.py` requests on one consent screen, so a third-party provider cannot ride it — `oauth=None` and the flow lives in the package's own `routes.py` (`/api/strava/connect?user=<name>`), mounted via `MANIFEST.routes`. ⚠️ **One kernel edit is unavoidable and was found only by running the flow**: `/api/strava/callback` had to be added to `app/main.py`'s `AUTH_EXEMPT`, because `ui_token` is SameSite=Strict and so is not sent when Strava redirects the *browser* back cross-site — gated, the callback 401s and the grant the user just approved is lost, looking like a Strava fault. `/api/auth/google/callback` carries the same exemption for the same reason, so this is a pre-existing hole in the plugin contract, not a Strava quirk: nothing in a manifest can declare "this route authenticates itself". Worth a `public_routes` manifest field if a third provider appears. `connect` deliberately stays gated (it names the user a token is attributed to, and a prefix-wide exemption would have taken it with it); two tests pin both halves. Tokens go in the existing provider-agnostic `oauth_tokens` table as `provider="strava"`. ⚠️ **Scope is `activity:read_all`, and the callback rejects anything less.** `activity:read` silently omits every 'Only You' activity — no error, no count discrepancy, just a smaller archive — so accepting a narrower grant would produce a permanently incomplete history that reports success. ⚠️ **Strava rotates the refresh token on every refresh** and access tokens live only six hours, so the whole token response is written back, not just `access_token`; persisting only the latter yields a row that works for six hours and is then dead. Credentials are deliberately **not** `required` config — that would gate the read-only tools off via `is_configured()` — so `sync._credentials()` raises a `PermanentError` naming the missing keys instead, and an unconnected Strava is a clean no-op because `accounts()` returns `[]`. **No staleness probe, deliberately**: `start_date` staleness just asks whether someone exercised recently (a fortnight off is a holiday, not a fault) and `synced_at` staleness duplicates `SyncState`. `strava_backfill` walks the full history backwards via `before=`, not by page number — page numbers shift under concurrent uploads and silently skip activities — checkpointing to `sync_cursors` so a rate limit is resumable and reported as `rate_limited`, never as `complete`. |
| `finance` | None (manual) | 12 | CSV import (AIB, Revolut) | 4,683 txns, 669 rules, 98.4% coverage |
| `obsidian` | */30 * * * * | 4 | Local vault files | pgvector + fastembed, per-user vault trees |
| `whatsapp` | */30 * * * * | 7 | Baileys bridge (Node.js sidecar) | 134,144 messages (2026-08-17), conversation-window embedding — **99.4% of text messages covered** (111,347 of 112,021, by summing each chunk's `message_count`; ⚠️ `whatsapp_stats.total_embedded` counts *chunks*, so dividing it by `total_messages` compares different units and understates coverage ~12x). **Message-to-self chats are the documented exception to conversation-window chunking** (2026-08-17): each note becomes its own chunk, since `_merge_runts` has no distance limit and was gluing a note typed today to an unrelated one from days earlier. Config `whatsapp_self_chat_jids` is **`{user_id: jid}`** — ⚠️ a WhatsApp `@lid` is scoped to the account that observed it, *not* global (the same LID matched 178 rows under the second bridge, all messages *received* from a third party), so matching is on the pair **and** requires `is_from_me`. |
| `weather` | */30 * * * * | 2 | Open-Meteo API (no key) | Location from `weather_latitude`/`weather_longitude` config. Not `required`: tools read cached rows and stay usable; only `sync()` raises a `PermanentError` naming the missing keys. |
| `lastfm` | */15 * * * * | 5 | Last.fm API | 50,000+ scrobbles |
| `irish_rail` | None (live) | 2 | Irish Rail XML API | Default station from `rail_station_code` config; `station` is still a per-request argument, so tools work unconfigured and return a message naming the fix. No caching. |
| `homeassistant` | WS events (real-time) + */5 poll reconcile | 6 | Home Assistant REST + WebSocket (192.168.1.51) | Gap-free: WS `state_changed` listener (lifespan task, `events.py`) feeds `ha_entities`/`ha_state_changes` live; the 5-min poll reconciles after downtime + refreshes areas. Numeric ticks excluded from history by default — two ways in, and the narrow one is almost always right: `ha_numeric_history_entities` (a per-entity allowlist, added 2026-08-22 because a deriver's `features()` runs against historical timestamps and can only train on entities whose history is kept) or `ha_record_numeric_history` (the global firehose over ~1,700 entities). `ha_backfill_history` + `backfill.py` import HA's own recorder for the allowlist — without it a newly-allowlisted entity has no past, so a new deriver is blind for its whole training window; bounded by HA's `purge_keep_days` (default 10), idempotent on `(entity_id, changed_at)`. Signal curation in `tools.py::SECTIONS`. `client.py` also gained `call_service(domain, service, data)` (typed `TransientError`/`PermanentError`, unlike this module's other log-and-swallow calls) and `facade.notify(target, title, message, data=None)`, exposed as a new `homeassistant.notify` capability (`provides=["homeassistant.entities", "homeassistant.notify"]`) — first and so far only consumer is `notifications`' HA push sink. |
| `solar_forecast` | `20 * * * *` (predict) + `40 4 * * sun` (train) | 2 | comar's own HA cache (no external call) | **The first `type="deriver"`** — built on `app/algo/`, owns no tables. Forecasts PV generation 1–12h ahead from the inverter's recent output plus Forecast.Solar's recorded estimate, and is graded hourly against what the panels actually produced. It *consumes* Forecast.Solar rather than replacing it: what it can learn that a generic irradiance model cannot is that forecast's **local** bias (roof pitch, tree line, 5.5 kW clipping). Baseline is hour-of-day climatology, not persistence — "as much as right now, in six hours" would prove nothing. Nights are skipped (trivially zero on both sides, so including them makes MAE look excellent and skill look like nothing). ⚠️ Idle until configured — see its README; the silence is correct, not broken. |
| `system` | None | 5 | Cross-integration diagnostics | Health alerts, morning briefing, week ahead, search everything, and **`system_ai_usage`** (2026-09-02: what AI calls cost, by role and model, with the recent calls in full — reads the `ai_usage` ledger; NULL cost surfaces as `unpriced_calls`, never as zero). `CapabilityService` — composes other integrations' facades, no tables of its own. |
| `media` | 5,35 * * * * | 4 | Baileys bridge `/download/:id` | WhatsApp media store: indexes ALL images/videos/audio in `media_items`, auto-downloads last ~30 days to `HOME_MEDIA_ROOT` volume, `media_export` copies into the vault for note-embedding. Documents stay with `attachments`. **Host dir must live OUTSIDE `~/lios-core/`** (rsync deploy uses `--delete`). |
| `snags` | None (user-gated capture) | 5 | WhatsApp `Snag - …` messages + manual | Snag register — **DB is source of truth**, immutable UIDs (`SNAG-0042`), trade/severity/status lifecycle, evidence via `snag_media` → media store. Vault note `Household/Renovation/Snags.md` is a generated one-way view (re-rendered on every write; evidence auto-exported to `Attachments/Snags/UID-n.jpg`). Also mirrors into a shared Google Sheet — see `sheets` below. Trades, trade labels and room aliases are **deployment config** (`vocab.py`, 2026-07-28), not code constants — `trades` drives the `trade` validation, so it must list every value already in the table. |
| `sheets` | None (write-only, invoked by other integrations) | 0 (no tools of its own) | Google Sheets + Drive API | A `CapabilityService` (`provides=["sheets.write"]`) rather than a bare library — a reusable "push a table of rows to a Sheet" writer (`app/integrations/sheets/writer.py`, exposed to other integrations via `facade.py`) for household members without MCP/vault access. Creates the spreadsheet once per `key` (tracked in `sheet_exports`), shares it with `HOME_SHEETS_SHARE_WITH` emails, overwrites wholesale on every call. First consumer: `snags/tools.py::_render` mirrors the snag register on every add/update, best-effort (a Sheets outage never blocks the underlying DB write). Requires `HOME_SHEETS_OWNER_ACCOUNT` set and that account's OAuth token to carry the `spreadsheets` + `drive.file` scopes (added 2026-07-20 — see Known Issues). |
| `embedding` | None (worker, not cron) | 2 | N/A — kernel-shared service | `CapabilityService` owning the unified `embeddings`/`embedding_queue` tables and provider interface (moved out of `app/services/embedding.py`, V4 chunk 3.4 — that module now just re-exports for back-compat). |
| `attachments` | */30 * * * * | 4 | Gmail/WhatsApp message attachments | Downloads, parses, and embeds message attachments into the historical corpus. Manifest type is `capability` but it behaves like a `SourceIntegration` (runs a real scheduled scan) — a documented, deliberate mismatch (see `writing-an-integration.md` §7). |
| `coffee` | None (manual) | 12 | Manual brew/bag logging | Coffee bag/brew log with dial-in advice and semantic search over brew notes. `CapabilityService` — tool surface over its own tables. |
| `commute` | 0-57 7-8 * * 1-5 (weekday mornings) | 3 | NTA GTFS-Realtime + Irish Rail live data | Weekday-morning bus→rail commute solver. The **route is config** since 2026-07-28 (`commute/routing.py` builds both `Route`s from `commute_bus_*`/`commute_rail_*` keys); `domain.py` stays pure. Models one bus leg + one rail leg meeting at a single interchange — a different journey shape is a solver rewrite, not a config key. |
| `historical_corpus` | None (manual ingest) | 3 | Household document corpus (scans, PDFs, attachments) + claude.ai conversation exports | pgvector semantic search over ingested documents. Primary tool is `corpus_search` (renamed from `riverside_context` 2026-07-28 — a tool name is public API and must not carry a family project). Default `project_tags` comes from the `default_project_tag` config key. New: `ingest_claude_export()` / `scripts/ingest_claude_export.py` parses claude.ai conversation-export JSON, `claude_history_search` MCP tool. `CapabilityService`. |
| `inbox` | 7 * * * * | 6 | Vault `Inbox/` landing zone | Scans for dropped files and routes them into `historical_corpus`; also serves the capture ingest route. **⚰️ Tines retired 2026-08-29** — the "Dictator" story is deleted and `POST /api/inbox/ingest` is now reached directly by the iOS/macOS Shortcuts over a Cloudflare Tunnel (`ingest.comar.ie`, Access service token per device + per-person `client_tokens` bearer; see `server/docs/capture-clients.md` and `infra/docs/cloudflare-tunnel.md`). Tines had been a *second* transcription implementation — its own prompt, its own model pin, its own hand-maintained proper-noun list — and all three had gone stale with nothing comparing them to comar's. **Enrichment fixed 2026-07-31** — Tines posts a bare-UUID filename with *no extension* and no metadata, so kind detection falls entirely to magic bytes: the ISO-BMFF check was `startswith(b"\x00\x00\x00 ftyp")`, matching only a 32-byte ftyp box, so iOS voice notes sniffed as `unknown`. Now checks bytes 4:8 for `ftyp` and splits audio/video on the major brand (`M4A ` etc). Audio/video previews report duration via a dependency-free `moov/mvhd` parser (**no transcription** — that's the voice-memo integration). `scan.summarise()` renders the one-line description, `metadata.note` (a caller-supplied transcript) leads it, and `POST /api/inbox/ingest` now enriches inline and returns `summary`/`kind`/`preview`/`note` so the caller's push notification can say something true. New `facade.py` (`enrich_for_response`) because the kernel route may not import `inbox.scan` directly. **2026-08-17 — three additions.** (1) A saved **web page** is its own `html` kind, previewed via a stdlib `HTMLParser` that extracts `<title>` (it previously sniffed as `text`, so its preview was 500 bytes of doctype); detection requires the document to *open* with markup, not merely contain `<html>`, or a note discussing markup gets tag-stripped. (2) `describe_pending` now **announces** its result — it had always written the vision description into `note` but had no sibling to `_notify_transcribed`, so images were described and nobody was told; both paths share `_notify_enriched`. (3) New background task `inbox_route_whatsapp_notes` (`*/10`) pulls **WhatsApp message-to-self notes** into the queue via `depends_on=["whatsapp.query"]` — ⚠️ the inbox *pulls*, because whatsapp pushing here closes a cycle boot validation rejects (`whatsapp → inbox.ingest → notify.push → system.alerts → whatsapp.query`). Idempotency reuses `find_by_hash`, which searches *terminal* buckets, so a triaged-and-archived note can't return (a pending-only check makes 144 duplicates a day on this cron). Bounded by `inbox_whatsapp_note_max_age_days` (default 7) — routing only, never embedding, since search should reach all history. `inbox_confirm_push` (default off) lets the server announce an ingest itself — this was the last job holding the Tines relay in the capture path, and **that relay is now gone (2026-08-29)**. Transcription also fires as a `BackgroundTask` straight off ingest, so a capture is readable in about a minute; the `*/5` cron is now the retry/sweeper net rather than the primary path. A transient failure is retried to `MAX_TRANSCRIPTION_ATTEMPTS` (3) and a give-up notifies **and emails** (`notify.email`) — before this, `transcribed_at` was stamped before the outcome was inspected, so one Gemini 503 buried a memo permanently and silently. **2026-08-30 — the capture notifications, fixed.** (1) **Owner routing.** `_notify_enriched` called `notify.push.send()` with no `user_id`, i.e. household-wide, i.e. — per the `notifications` row below, which already said so — *one phone*. Every capture notification went there regardless of who captured it, so one person's memos notified another and the name-free `title` landed on the wrong lock screen. The owner was never unknown: the transcript **email** two frames away was already resolving it via `owner_user_id_from_path`. 🔑 Now derived inside `_notify_enriched` from the path rather than passed in as an argument — an optional `user_id` that four call sites must remember is exactly the shape that produced the bug, since omitting it fails silently and means "household". (2) The **give-up notice names the recording** — Tines' body was "No usable transcript was produced. Please try again." and nothing else, unactionable with two memos in a morning, and "try again" wrongly implies the audio is gone. (3) **A captured document now gets a second beat**: `_notify_document_email` sends the extracted text with the original attached under `EMAIL_ATTACHMENT_MAX_BYTES` (oversize drops the attachment, never the message). Both capture emails render through one `_capture_email_html`. |
| `notifications` | None (cron background task, `*/15`) | 2 | Writes to Home Assistant mobile-app push (`homeassistant.notify`) | **Added 2026-07-31**, sink swapped from a self-hosted ntfy topic to HA mobile-app push 2026-08-13 (`ae78ae6`) — same `notify.push` contract, `sweep.py` and the ledger untouched, only `client.py`'s transport changed. Reason: one less standing service to run, and HA's companion app gives per-device routing (critical alerts, per-user targets) for free. The push sink `system_alerts` never had — alerts had been populated and on the dashboard since 2026-07-15 but reached nobody's phone. `sweep.py` reads the alert payload via the `system.alerts` capability, fingerprints each distinct problem, and publishes only the delta against its `notification_sends` ledger (one open row per fingerprint, partial unique index). Fingerprints key on issue *kind*, never rendered text — the text carries an age that changes every sweep. `provides=["notify.push", "notify.email"]` for other integrations (best-effort, never raises into a caller's write path); `depends_on=["system.alerts", "homeassistant.notify"]`. No `staleness_probe` by design: a quiet ledger is the healthy state. Config (`targets`/`household_targets`) is **not** `required` — cron background tasks run regardless of `is_configured()` (`scheduler.py:217`), so `client.py` enforces it at the call site with a `PermanentError` naming the keys; both default empty to satisfy the personalisation guard. ⚰️ **ntfy is decommissioned as of 2026-08-14** — the last publisher (a Tines voice-note story) was repointed at HA and `homelab-ntfy` was removed; see `infra/CLAUDE.md`. **2026-08-29 — `notify.email` added**, a second capability on this same integration: stdlib `smtplib` over the SMTP2GO account `webmigration` already set up (no new dependency, `comar.ie` already DKIM-verified). It exists because retiring Tines removed the only thing that emailed a finished transcript, and `google_mail` is read-only. Recipients come from an `email_targets` config dict keyed by user_id — mirroring `targets` for push — because there is **no `email` column on `User`** and this needed no migration. Best-effort in the same way `send()` is: the transcript is already saved, so a mail outage must never fail or re-queue the capture. ⚠️ Note `publish()` does **not** write the ledger — only `sweep.py` does, so `notify_recent` cannot confirm an ad-hoc push was sent (measured 233 → 233 across a send). **2026-08-19 — two additions.** (1) **Per-user routing is live.** Alerts are now attributed to an owner *structurally* (`system/tools.py::_attribute` writes an `issue_users` map onto each alert entry, keyed on the issue string) rather than by regexing prose, which previously covered only the health-coverage shape — so per-owner staleness rows and daemon liveness were household-shared as far as the sweep could tell. `_publish` passes `user_id` through to `client.publish`, falling back to household when that person has no `targets` entry. New config `suppress_push_for_user_ids` drops one member's attributable alerts **at the push boundary only** — they stay in `system_alerts` and on the dashboard, because filtering them out of `check_all` would recreate the blindness the per-owner probes were added to fix. ⚠️ `household_targets` holds **one** phone here, so "household-wide" has always meant one person; that is why another member's stalled laptop reads as noise. (2) **Deadline watches** (`deadlines.py`) — a wall-clock question no staleness threshold can express: "it is past 10am and last night's sleep still isn't here". Expressed as an alert item that exists only while the condition holds, so the existing ledger supplies once-a-day dedupe and the recovery ping for free. ⚠️ It lives here, not in `apple_health`, because `apple_health → notify.push` would close a cycle (`notifications → system.alerts → health.query`) that boot validation rejects — the package that pushes has to be the one that pulls, same as `inbox` pulling WhatsApp notes. ⚠️ **The `household_targets`-is-one-phone note above was written before anything acted on it.** It was accurate from 2026-08-19 and the capture path went on sending household-wide for eleven days — a documented hazard is not a fixed one, and prose in this file cannot route a notification. Fixed in the `inbox` row's 2026-08-30 entry. 🔑 Related trap when testing any of this: `_notify_enriched` swallows `Exception` so a dropped push never fails a capture, which means a test double whose `send()` signature has drifted from `NotificationsFacade.send` raises no visible TypeError — it records **zero pushes**, and every assertion reads "not sent" when the truth is "the double is stale". Keep `tests/test_inbox_enrichment.py`'s `_Notify` in step with the facade. |
| `transcription` | None (driven by callers) | 0 | Gemini (`stt.memo` AI role) | **Added 2026-07-31.** `provides=["transcription.audio"]`; owns no tables, no schedule, no tools — whoever holds the audio drives it (today `inbox`'s `*/5` cron). **One path, always the diarizing model**, with speaker labels dropped automatically when only one speaker is detected. Considered and rejected: routing solo vs conversation to a cheaper model. Speaker count is unknowable before transcribing; a pre-check needs decoded PCM (ffmpeg in the image) plus a heuristic that silently loses attribution when wrong; and the premium is only $0.006 vs $0.0045/min — ~£1/year here. A capture-time hint was built and then removed for the same reason: not worth two code paths. `embedded.py` reads Apple's on-device transcript from the `tsrp` atom (`moov>trak>udta`) for free, so `prefer="embedded"` makes a backfill pay only for genuine gaps (~70 of 373 memos, not 342); on the live path it's the fallback for a network blip. **Gemini-only since 2026-08-27** (PR #27 deleted the OpenAI path); the model id lives in the `stt.memo` AI role, not inline — `gemini-3.7-flash` since 2026-08-29 (benched: half the cost, half the latency; ⚠️ **never `gemini-3.5-flash-lite`**, which reported `speakers: 2` and returned one merged unlabelled block, a diarisation failure character-agreement scored 0.948 and could not see). Structured JSON output carries `title` and `speakers` alongside the transcript; `title` is deliberately **name-free** because it renders on a phone lock screen and in an email subject. **Proper-noun prompt reads the whole vault, not just People notes (2026-08-29).** People notes stay authoritative — their `aliases` are actual recorded mis-transcriptions — and are joined by terms mined on *document frequency* across every note, minus ordinary English words. Measured on a real memo: 9 of its 47 proper nouns covered before, 18 after (`Wicklow` occurs in 51 notes, `UniFi` in 42, and neither had a People note). ⚠️ The English filter needs `/usr/share/dict/words`; `python:3.12-slim` has none, so the image installs `wamerican` and a missing wordlist logs at WARNING — without it the mining is a silent no-op. ⚠️ Known cost: web2 is inclusive enough that `niall`, `tiff` and `polestar` are *in* it and get filtered; People notes and `extra_dictionary_terms` bypass the filter, which is the escape hatch. Ported from `sandbox/voice-memos/`. |

MCP tool counts above are each integration's `mcp_tools()` output length (unconditional — actual registration at boot also gates on `enabled` + `is_configured()`, so a disabled/unconfigured integration registers none). Total across all 26 packages: **126 tools**, measured 2026-08-20 by instantiating `register_all()` and summing `mcp_tools()` (`sheets`, `transcription` and `vision` add none — pure facades). ⚠️ The figure this line carried before that measurement was **119**, and 119 was already wrong by two before `google_docs` added five — so it had drifted between 14 and 20 August with nobody touching this line. Measure it; do not quote it. (The pre-2026-08-13 figure, "~107 across 23", was stale by two integrations and twelve tools — this line has now drifted twice.)

## MCP Server

Stateless **Streamable HTTP** transport at `/mcp/`. Per-user bearer only — every caller needs a real `client_tokens` row or a valid OAuth 2.1 access token; the shared-secret `HOME_MCP_TOKEN` admin fallback (synthesised user_id=1 for any bearer matching a single env var) was removed in V4 chunk 2.3.

Tools are auto-discovered from each enabled, configured integration's `mcp_tools()` method at startup (`register_mcp_tools()` skips an integration if `is_integration_enabled()` is false or `is_configured()` is false). Every tool must carry inline `annotations` — `MissingAnnotationsError` fails server startup otherwise (the old centralized `app/mcp/annotations.py` fallback is gone). Tool handlers run in `asyncio.to_thread()` to avoid blocking the event loop. Naming convention: `integration_action` (e.g. `calendar_today`, `finance_summary`). Per-user request scoping is pinned via `current_user_id()` (ContextVar) for the duration of each handler.

134 tools across the 28 integration packages below (measured 2026-08-29). This line had been left at 126/26 since 2026-08-20 while the heading above said 129/27 — two counts of the same thing, in one file, disagreeing. Both were also wrong: the real pre-strava figure on `main` was 130. See the vault `CLAUDE.md` for the full tool reference table.

## Scheduler

APScheduler `AsyncIOScheduler` registered in `scheduler.py`. Scheduling is manifest-driven (V4 chunk 3.1) — each integration's `schedule`/`schedule_timezone` in its own `manifest.py` is the single source of truth (`None` means no scheduled job); `background_tasks` (`TaskSpec`, startup long-runners or extra cron jobs) are also declared there and wired up by the kernel with zero per-integration scheduler code. Registration also checks `is_integration_enabled()` — a disabled integration gets no cron job.

- **Timeout**: 5 minutes per sync (`asyncio.wait_for`)
- **Overlap prevention**: `max_instances=1` per integration
- **Misfire handling**: `misfire_grace_time=120` (skips if >2 min late)
- **State tracking**: Writes to `SyncState` table after every sync (ok/error/timeout) — visible via `/api/integrations/`
- **Kernel-owned jobs** (`app.plugin.kernel_jobs.KERNEL_JOBS`): the three daily audit prunes, plus `score_algo_predictions` at `12 * * * *` — grades every `type="deriver"` integration's due predictions against observed reality. Offset off the hour because every other cron here fires on `:00` and scoring reads the tables those jobs write. Each deriver is scored in its own try block, so one broken `observe()` never stops the rest being graded.

## Database

Postgres 16 with pgvector extension (auto-created on startup in `db.py`).

Uses coglib pattern: models inherit `coglib.Base`, sessions via `db.session()` context manager or `SessionDep` FastAPI dependency.

### Key Tables

| Table | Integration | Purpose |
|-------|-------------|---------|
| `users` | Core | Identity table (id, name, display_name). Seeded with Alex (1) + Sam (2) |
| `oauth_tokens` | Core | Google OAuth tokens — per-user (user_id, provider, account_email) unique, encrypted via Fernet |
| `sync_state` | Core | Last sync time/status per integration |
| `client_tokens` | Core | Per-device bearer tokens (user_id, label, last_seen, version) |
| `client_logs` | Core | Remote log shipping from clients (user_id, level, logger, message, timestamps) |
| `algo_predictions` | Core (algo harness) | One claim per (algo, quantity, `target_at`, `horizon_min`) — that combination is the unique constraint, and `app/algo/predictions.py` names it in an `ON CONFLICT`, so renaming it breaks recording at runtime. `made_at` **and** `target_at` because a prediction is only verifiable later; `actual`/`error`/`scored_at` are filled in by the kernel scoring job |
| `algo_model_versions` | Core (algo harness) | Fitted models as JSONB params + the feature order they were fitted against. `is_active` picks the serving version; a fit is saved inactive and activated deliberately |
| `algo_runs` | Core (algo harness) | One row per predict/train/score execution — the deriver equivalent of `sync_state`, plus LLM tokens and cost |
| `calendar_events` | google_calendar | Cached calendar events |
| `mail_messages` | google_mail | Cached email metadata (subject, sender, date, labels) |
| `mail_embeddings` | google_mail | LEGACY — migrated to unified `embeddings` table |
| `reminders` | apple_reminders | Synced reminder items |
| `reminder_commands` | apple_reminders | Command queue backing the current EventKit SSE dispatch/ack path (`commands.py`); also queried directly by `backlog_sync` |
| `accounts` | finance | Bank accounts (AIB, Revolut) |
| `categories` | finance | Spending categories (hierarchical) |
| `categorization_rules` | finance | Pattern → category mapping rules |
| `transactions` | finance | Financial transactions |
| `import_history` | finance | CSV import dedup via file hash |
| `monthly_summaries` | finance | Pre-computed monthly aggregates |
| `vault_chunks` | obsidian | Vault file hashes + modification times (incremental indexing tracker). **Per-user** (`UserOwnedMixin`, unique on `(user_id, path)`) — one row set per `/vaults/<user>/` tree |
| `weather_current` | weather | Current conditions (single row, replaced each sync) |
| `weather_forecasts` | weather | 7-day daily forecast (upsert by date) |
| `scrobbles` | lastfm | Music listening history |
| `ha_entities` | homeassistant | Latest state per HA entity (upsert each 5-min sync) |
| `ha_state_changes` | homeassistant | Append-only non-numeric state transitions (appliance cycles, switches, presence) |
| `media_items` | media | WhatsApp media store index (status, storage_path, sha256; UserOwnedMixin) |
| `snags` + `snag_media` + `snag_source_messages` | snags | Snag register (household-shared): UID from `snag_uid_seq`, evidence links, idempotent capture tracking |
| `notification_sends` | notifications | Alert send ledger — one **open** row per fingerprint (partial unique index `WHERE resolved_at IS NULL`), resolved history unconstrained so a recurring problem gets a fresh row per episode. Household-shared (no `UserOwnedMixin`): infrastructure alerts, single configured topic. |
| `sheet_exports` | sheets | One row per Sheets export `key` (e.g. `"snags"`) — spreadsheet id/url, owner account, share list, last synced. Household-shared like the tables it mirrors. |
| `whatsapp_messages` | whatsapp | Messages captured by bridge (chat_id, sender, body, media) |
| `whatsapp_contacts` | whatsapp | Contacts and groups with last message times |
| `embeddings` | Core | Unified embedding store (pgvector 384-dim, source-keyed) |
| `embedding_queue` | Core | Pending items for embedding worker (source, status, text) |

### Model Registration (manifest-driven since V4 chunk 1.2)

Integration models are no longer imported by hand in `app/models/__init__.py`. Each integration declares its ORM class names in its own `manifest.py::MANIFEST.models`; `app.plugin.discovery.discover_integration_models()` resolves and imports them at `app/models/__init__.py` import time, so `create_tables()`/Alembic see every integration's models with zero edits to that file. Only kernel-owned models (`User`, `OAuthToken`, `SyncState`, `ClientToken`, `ToolCall`, `AuthEvent`, OAuth-client tables, `SyncCursorRow`, `IntegrationConfig`, etc.) are still imported explicitly there. A new integration only needs its models listed in its own manifest — forgetting that entry now fails loudly at boot validation (`app.plugin.validate`), not silently at `create_tables()` time.

### Multi-user pattern (UserOwnedMixin)

Per-user tables apply `UserOwnedMixin` from `app/mixins.py` — one column `user_id INT NOT NULL FK → users.id ON DELETE RESTRICT, indexed`. Composite uniques start with `user_id` (e.g. `(user_id, uid)`).

Tables that have it: `client_tokens`, `client_logs`, `oauth_tokens`, `reminders` (+`account_email` for EventKit multi-account routing), `reminder_commands`, `mail_messages`, `scrobbles`, `whatsapp_messages`, `coffee_brews`, `message_attachments`, `health_daily_metrics`, `health_workouts`, `health_sleep_sessions`, `vault_chunks` (per-user since 2026-07-25 — vaults are one-per-user on disk, so the index must be too).

Shared / household-scoped tables deliberately don't take the mixin: finance (joint), `historical_documents`, `weather_*`, `artist_tags` (community metadata), `coffees` (the bag, shared), `coffee_equipment_profiles`, `whatsapp_contacts` (global graph).

When adding a new table: per-user is the safer default. See user-memory `feedback_useowned_mixin_pattern.md`.

## Authentication

### Web UI
Cookie-based bearer token (`HOME_UI_TOKEN`). Set on login, checked by middleware on all `/api/*` routes. 30-day expiry.

### MCP + HTTP API — per-user bearer only (V4 chunk 2.3)
Each daemon Mac (or OAuth 2.1 connector session) resolves to a real row: `client_tokens` (FK → `users.id`) or a valid `McpAccessToken`. `app/auth/client_token.py::get_current_user` / `app/mcp/server.py::_authenticate_request` validate the bearer and return the **detached `User` model** (not just a string) — snapshot attrs before commit, `expire_on_commit=True` will bite anyone who tries to read user attrs after the session closes. Tokens are hashed at rest (`token_hash`, `app/auth/hashing.py`) with an `expires_at` (default TTL, extended on use) — plaintext is never stored and never logged (only `token_last4` appears in log lines). The old shared-secret `HOME_MCP_TOKEN` admin fallback (synthesised user_id=1 for anyone holding one env-var secret — a standing backdoor into Alex's data) was removed entirely in this chunk; there is no unauthenticated/dev-mode MCP path anymore. Every auth attempt (success or failure, and why) is recorded to `auth_events` (V4 chunk 2.5) for audit.

### Legacy Mac-agent ingest routes (sam-rollout A2)
`/api/reminders/backlog-sync` and `/api/health/push` (distinct from the live `/api/v1/*` push routes) used to gate on the shared `HOME_UI_TOKEN` secret and hardcode `user_id=1`. They now resolve the caller via the same per-user `client_tokens` bearer dependency (`get_current_user`) and reject with 401 rather than defaulting. The third route this originally covered, `/api/reminders/sync`, was removed 2026-08-08 as confirmed dead code — the live daemon already calls the newer `/api/v1/reminders/push` path, and the standalone `scripts/reminders-sync/sync.py` agent that was the scheme's only other caller was never deployed and was removed in the same sweep.

### Audit trail (V4 chunk 2.5)
Every MCP/API tool call is recorded to `tool_calls` (name, args, actor/user, duration, outcome) via the single dispatch chokepoint (`app.plugin.dispatch`). Every auth attempt is recorded to `auth_events` (outcome, token last-4, source IP, transport). `system_alerts`/`GET /integrations/{name}/tools` surface recent call counts from this table.

### Per-user request scoping
`app/auth/context.py::current_user_id()` is a ContextVar pinned by `v1.py::call_tool` via `use_user(user.id)` for the duration of each handler. DSL builders (ListTool/SearchTool) auto-detect `hasattr(model, "user_id")` and inject `WHERE user_id = current_user_id()`. Hand-written tool handlers call `current_user_id()` explicitly. Cross-user dashboard summaries in integration `__init__.py` files deliberately skip scoping (admin household view).

### Google OAuth
Multi-account, multi-user. `/api/auth/google/login?account=<email>&user=<name>` round-trips the user through Google `state`. Callback writes the token row with `(user_id, provider="google", account_email)` unique. Tokens stored encrypted via `app/auth/encryption.py` (Fernet, gracefully passes through plaintext if `HOME_OAUTH_ENCRYPTION_KEY` unset). Manual URL construction (avoids google_auth_oauthlib PKCE issues).

**Private IP workaround**: Google rejects RFC 1918 IPs as redirect URIs. Set `HOME_OAUTH_REDIRECT_BASE=http://localhost:8400` and use SSH tunnel (`ssh -L 8400:localhost:8400 ubuntu`) during OAuth re-auth. Add `http://localhost:8400/api/auth/google/callback` to Google Cloud Console.

## API Endpoints

### Core

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/dashboard/summary` | GET | All integration data in one call |
| `/api/integrations/` | GET | List integrations with sync status, `configured`, and `enabled` (V4 chunk 5.1) |
| `/api/integrations/{name}/sync` | POST | Trigger manual sync |
| `/api/integrations/{name}/detail` | GET | Manifest-driven integration hub page: flows (reads_from/writes_to/embedding_sources), capability deps, OAuth scopes, background tasks, history (V4 chunk 5.1) |
| `/api/integrations/{name}/enabled` | PUT | Body `{"enabled": bool}` — flip the integration's kernel-level enable switch (gates MCP tool registration + scheduling on next process start) |
| `/api/integrations/{name}/tools` | GET | Tools this integration currently registers, with annotations + recent call counts from `tool_calls` |
| `/api/integrations/{name}/config` | GET/PUT | Read (secrets masked `•••last4`) / upsert this integration's `config_schema` values in `integration_config` |
| `/api/auth/tokens` | GET | List connected OAuth accounts |
| `/api/auth/google/login?account=email` | GET | Start Google OAuth flow |
| `/api/auth/google/callback` | GET | OAuth callback (exchanged code for tokens) |
| `/api/health` | GET | Health check |

### V3 client API (`/api/v1/*`, per-user bearer)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/commands` | GET | sam-rollout B2 — the caller's curated `.claude/commands/*.md` set (daily-note, add-task, find, triage, lock-in, week-ahead) + rendered CLAUDE.md, generated from `app/prompts/templates/*.md.j2`. Used by the installer and the daemon's optional startup refresh. |
| `/api/v1/instructions` | GET | sam-rollout D1+D2 — per-user rendered MCP instructions: household-shared core + the caller's own "Your Setup" section (display name, vault paths, only the private integrations they have data for via each facade's `has_data()`, voice-profile guidance), 5-min TTL cache. The bare MCP-handshake `instructions` stays the static shared core. |

### Apple Reminders / Apple Health (Mac agent, legacy routes)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/reminders/backlog-sync` | POST | Trigger vault backlog ↔ Reminders sync for the calling user. Same per-user bearer requirement as above. |
| `/api/health/push` | POST | Bulk Apple Health push (distinct from `/api/v1/health/push`). Same per-user bearer requirement as above. |

### Client Distribution (no auth — LAN only)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/client/version` | GET | Latest client version + wheel filename |
| `/api/client/download/latest` | GET | Download latest client wheel |
| `/api/client/download/{filename}` | GET | Download specific wheel |
| `/api/client/bootstrap.sh` | GET | Bootstrap script (curl-able, auto-installs client) |

### Backfill & Maintenance

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/integrations/google_mail/backfill?after_date=2021/01/01` | POST | Backfill Gmail history |
| `/api/integrations/google_mail/embed` | POST | Embed un-embedded messages |
| `/api/integrations/lastfm/backfill` | POST | Backfill all scrobble history |

## Deployment

### Commands

```bash
make dev              # Local: uvicorn --reload + vite dev
make deploy-pull      # DEFAULT: pull pre-built GHCR images + restart (root `make deploy` points here)
make deploy-build     # Dev rsync path: build frontend + rsync + docker compose up --build
make deploy-fast      # Dev rsync path, backend only: rsync + docker compose up --build app
make stage-libs       # Copy lios/libs/coglib into .libs/ for the Docker build (build/docker-up/deploy-fast depend on it)
make server-logs      # Tail app container logs
make server-status    # Show container status
make server-restart   # Restart app container
make server-down      # Stop everything
make server-init      # First-time: create dir, copy .env template
make db-migrate       # Generate Alembic migration from model diff
make db-upgrade       # Run pending migrations to head
make db-downgrade     # Downgrade one migration
make db-history       # Show migration history
make db-current       # Show current migration revision
```

### How deploy works

**CI deploy** (`make deploy-pull` — the default; root `make deploy` delegates here):
1. Push to `main` triggers GitHub Actions (`.github/workflows/deploy.yml`)
2. CI builds frontend, builds Docker images, pushes to `ghcr.io/cograda/lios-core` and `ghcr.io/cograda/lios-whatsapp`
3. `make deploy-pull` → SSH into server, `docker compose pull && docker compose up -d`

**Dev rsync deploy** (`make deploy-build`, or `deploy-fast` for backend-only) — use only to test uncommitted changes on the box without going through CI:
1. `make build` — runs `npm run build` in `frontend/`, outputs to `frontend/dist/` (skipped by `deploy-fast`)
2. `make sync` — rsyncs project + proto stubs to `ubuntu:~/lios-core/`
3. SSH into server, `docker compose up -d --build` — rebuilds the app image and restarts

coglib is **not vendored** any more (2026-09-02). It lives at `lios/libs/coglib`, the one copy in this repo; the Docker build cannot see `../../libs` so `make stage-libs` copies it into the gitignored `.libs/` first, and CI runs the same target before pushing the image. `tests/test_coglib_single_copy.py` fails if `server/coglib/` ever comes back.

### Docker architecture

- **db** container: `pgvector/pgvector:pg16`, persistent volume `pgdata`, healthcheck via `pg_isready`
- **app** container: Python 3.12-slim, installs coglib + requirements, copies backend + frontend/dist
- Port mapping: container port 8000 → host port 8400 (configurable via `HOME_PORT`)
- Vault mounted at `/obsidian` inside container (from `HOME_OBSIDIAN_HOST_PATH` on host)
- Fastembed model cache persisted in `fastembed_cache` volume

### Server details

- **Host**: `ssh ubuntu` (SERVER_IP, Proxmox VM)
- **Path**: `~/lios-core/`
- **HTTP API**: `http://SERVER_IP:8400` (LAN, no TLS), `https://comar.lab` (LAN via Caddy + internal CA), or `https://ubuntudockerbox.tail78010b.ts.net` (anywhere via Tailscale, browser-trusted LE cert)
- **Dashboard**: `https://comar.lab` (Caddy proxy from infra project)
- **Remote MCP**: Off-LAN clients use the Tailscale URL — same `/mcp/` path, same per-user bearer. Routing for that hostname is a static block in `infra/Caddyfile`; cert is host-managed at `/opt/homelab/certs` and renewed weekly. See infra/CLAUDE.md.
- **Vault on server**: `/home/alex/vaults/` — per-user tree (`/home/alex/vaults/alex/`, and `/home/alex/vaults/sam/` once she onboards her own vault). Single-user vaults: the cross-user `/home/alex/vaults/shared/` tree and the `Shared/` logical namespace were retired 2026-06-05. Mounted into the app container at `/vaults`; `/home/alex/vaults/alex/` is also bind-mounted to `/obsidian` as a back-compat alias for callers not yet migrated to `app.services.vault_paths.resolve()`. Synced bidirectionally with each Mac via Syncthing (the server runs Syncthing on the host, paired per-device via `POST /api/v1/syncthing/pair`). The historical rclone+Google-Drive sync is retired.

### Transport stance (V4 chunk 2.5)

Port 8400 itself is **plaintext HTTP by design** — it is never the TLS boundary and must never be exposed beyond the tailnet/LAN:

- `http://SERVER_IP:8400` — LAN only, no TLS. Reachable from home Wi-Fi/VLAN and Tailscale, nothing else.
- `https://comar.lab` — TLS terminates at Caddy (infra project), LAN-only internal CA cert.
- `https://ubuntudockerbox.tail78010b.ts.net` — TLS terminates at Caddy too, browser-trusted LE cert via Tailscale, for off-LAN devices.

Both HTTPS hostnames reverse-proxy straight to the same plaintext 8400 — the app itself never sees or manages a cert. Never add a port-forward, Cloudflare tunnel, or any other route that puts 8400 on the public internet unfronted by Caddy; the app's own auth (bearer/OAuth) is not a substitute for the transport being private in the first place. Every `/api/*` response also carries `X-Content-Type-Options: nosniff` and `Cache-Control: no-store` (small blanket middleware in `main.py`) — hygiene, not a substitute for the network boundary above.

## Environment Variables

All prefixed `HOME_`. Stored in `.env` on the server (never committed). **Since V4 chunk 3.3, most per-integration config (API keys, feature flags) lives in the `integration_config` DB table instead** (Fernet-encrypted at rest for secrets), read via `app.plugin.config_store.plugin_config(name)` and edited via `GET/PUT /api/integrations/{name}/config`. The `HOME_*` env vars below fall back to being read once as the transition-period default (`python -m app.plugin.import_config` — `app/plugin/import_config.py` — does a one-time copy of any set `HOME_<KEY>` into the DB table) — genuinely kernel/bootstrap-only settings (DB connection, UI token, TLS port) stay env-only permanently.

| Variable | Required | Purpose |
|----------|----------|---------|
| `HOME_DATABASE__URL` | Yes | Postgres connection (overridden by docker-compose for container networking) |
| `HOME_DB_USER` | Yes | Postgres user (used by docker-compose) |
| `HOME_DB_PASSWORD` | Yes | Postgres password (used by docker-compose) |
| `HOME_UI_TOKEN` | Yes | Web dashboard access token |
| `HOME_OAUTH_ENCRYPTION_KEY` | For secrets | Fernet key encrypting OAuth tokens and secret `integration_config` values at rest. Fail-closed: writing a secret config value without this set raises rather than storing plaintext. |
| `HOME_TLS_PORT` | No | Web dashboard HTTPS port (default 8443) |
| `HOME_GOOGLE_CLIENT_ID` | For OAuth | Google OAuth client ID |
| `HOME_GOOGLE_CLIENT_SECRET` | For OAuth | Google OAuth client secret |
| `HOME_OAUTH_REDIRECT_BASE` | For OAuth | Set to `http://localhost:8400` for SSH tunnel auth |
| `HOME_SHEETS_OWNER_ACCOUNT` | For Sheets export | Google account whose OAuth token owns/writes exported Sheets (must hold `spreadsheets` + `drive.file` scopes). Empty disables all exports silently. Now an `integration_config` key on `sheets` (env fallback during transition). |
| `HOME_SHEETS_SHARE_WITH` | For Sheets export | JSON array of emails to share new exports with, e.g. `["sam@example.com"]`. Same env-fallback status as above. |
| `HOME_LASTFM_API_KEY` | For Last.fm | Last.fm API key. Now an `integration_config` key on `lastfm` (env fallback during transition). |
| `HOME_LASTFM_USERNAME` | For Last.fm | Last.fm username (your_username). Same. |
| `HOME_OBSIDIAN_VAULT_PATH` | For vault | Path inside container (default `/obsidian`) |
| `HOME_OBSIDIAN_HOST_PATH` | Docker | Host path to vault (mounted into container) |
| `HOME_ANTHROPIC_API_KEY` | For backlog sync | Anthropic API key (Haiku task matching) |
| `HOME_WA_SYNC_HISTORY` | No | Set `true` for one-time WhatsApp history backfill (default false) |
| `HOME_HA_URL` | For HA | Home Assistant URL (e.g. `http://192.168.1.51:8123`). Now an `integration_config` key on `homeassistant` (env fallback during transition). |
| `HOME_HA_TOKEN` | For HA | Home Assistant long-lived access token (dedicated "comar" token). Same. |
| `HOME_MEDIA_HOST_PATH` | Docker | Host dir for the WhatsApp media store (mounted at `/data/media`) |
| `HOME_HA_RECORD_NUMERIC_HISTORY` | No | Also record numeric→numeric transitions in `ha_state_changes` (default false; HA's recorder keeps numeric series) |
| `HOME_COMMUTE_BUS_HOME_STOP` / `_BUS_HOME_RETURN_STOP` / `_BUS_INTERCHANGE_STOP` / `_RAIL_INTERCHANGE_STATION` / `_RAIL_CITY_STATION` / `_BUS_ROUTES` | For commute | The route, extracted from hardcoded `Route` constants 2026-07-28. `sync()` and `commute_query` raise a `PermanentError` naming the missing keys if unset. `commute_interchange_buffer_min` replaces `commute_howth_buffer_min`. |
| `HOME_WEATHER_LATITUDE` / `HOME_WEATHER_LONGITUDE` | For weather sync | Forecast location, extracted from hardcoded constants 2026-07-28. Deliberately not `required`: read tools serve cached rows regardless, and only `sync()` raises a `PermanentError` naming the missing keys (visible on the dashboard via SyncState). |
| `HOME_WEATHER_TIMEZONE` / `HOME_WEATHER_LOCATION_NAME` | No | IANA tz for sunrise/sunset (default UTC); display name for output. |
| `HOME_RAIL_STATION_CODE` | For irish_rail default | Default departure-board station, e.g. `GSTNS`. Not `required` — callers can pass `station` per request, and unconfigured handlers return a message naming the fix. |
| `HOME_RAIL_STATION_NAME` | No | Display name for the station. |
| `HOME_TRADES` | No | JSON array of trades *offered* for new snags. Safe to narrow or leave empty: validation uses `vocab.allowed_trades(session)`, which unions this with the trades already present in the table, so existing rows never become un-editable. Empty falls back to `vocab.py::DEFAULT_TRADES`. |
| `HOME_TRADE_LABELS` | No | JSON object, trade slug → display label for the rendered note/Sheet. Missing entries title-case the slug. |
| `HOME_ROOM_ALIASES` | No | JSON object mapping how people type a room → canonical name. Empty just capitalises the input. |
| `HOME_DEFAULT_PROJECT_TAG` | No | `project_tags` applied to newly ingested corpus documents (default `household`). Existing rows keep their original tags. |
| `HOME_OPENAI_API_KEY` | For transcription | OpenAI key used by the `transcription` integration. Secret (Fernet-encrypted in `integration_config`). Unset means audio falls back to whatever transcript the recording already carries, and `facade.available()` reports False so the inbox cron no-ops quietly rather than logging a failure every 5 minutes. |
| `HOME_MODEL` / `HOME_MAX_FILE_MB` / `HOME_DICTIONARY_FROM_VAULT` / `HOME_EXTRA_DICTIONARY_TERMS` | No | Transcription model (default `gpt-4o-transcribe-diarize`), upload size cap (25 MB — OpenAI's limit; `chunking_strategy: auto` lifts the *duration* limit but not this), whether to build the proper-noun prompt from People notes' `aliases`, and extra terms for things with no People note. |
| `HOME_TARGETS` | No | JSON object mapping a user_id (string) to their Home Assistant `notify.<target>` service name, e.g. `{"1": "mobile_app_a_phone"}` — per-user alert routing. Empty default: a real device name here would fail `tests/test_personalisation_guard.py`. |
| `HOME_HOUSEHOLD_TARGETS` | No | JSON array of `notify.<target>` service names (no `notify.` prefix) that receive household-wide alerts — what the alert sweep fans out to, since it has no single owner. Empty default for the same reason as `HOME_TARGETS`. |
| `HOME_SUPPRESS_PUSH_FOR_USER_IDS` | No | JSON array of user ids (as strings) whose personally-attributable alerts should not be pushed, e.g. `["2"]`. Push boundary only — the alert stays in `system_alerts` and on the dashboard. |
| `HOME_SLEEP_DEADLINE_USER_IDS` / `HOME_SLEEP_DEADLINE_HOUR` / `HOME_SLEEP_DEADLINE_TIMEZONE` | No | The sleep-by-deadline watch (`notifications/deadlines.py`): which users to check (JSON array of id strings; empty disables), the local hour by which last night's sleep is expected (default 10), and the IANA zone that hour is read in (default `Europe/Dublin` — UTC would make "10am" mean 09:00 for half the year). |
| `HOME_RESEND_AFTER_MINUTES` / `HOME_NOTIFY_ON_RECOVERY` | No | How long an unresolved alert stays quiet after being sent (default 1440 — a daily reminder, not a per-sweep one), and whether clearing an alert also pushes a recovery message (default true, so a silent phone means healthy). |

`HOME_MCP_TOKEN` is gone — the shared-secret MCP admin fallback was deleted in V4 chunk 2.3; there is no env var that grants MCP access anymore, only real `client_tokens` rows.

Client config lives in `~/.config/lios/config.toml` on each Mac (created by `lios-sync setup`). Client tokens are stored in the `client_tokens` table (hashed, with expiry) — create via server admin or CLI.

## Conventions

- **Config**: `HomeSettings(CogSettings)` with `HOME_` env prefix, `__` nested delimiter (e.g. `HOME_DATABASE__URL`)
- **Database**: coglib pattern — `coglib.Base` for models, `db.session()` for sessions, `SessionDep` for FastAPI dependency injection
- **MCP tools**: Namespaced `integration_action` (e.g. `calendar_list_events`). Handler functions take `(session, arguments)` and return JSON strings. Mechanical tools (list, search, semantic search, stats) use the declarative DSL in `app/tools/`. Domain-specific and admin tools stay hand-written, wrapped in `CustomTool`.
- **Migrations**: Alembic for schema changes. `db.py` auto-runs migrations on startup (three-way: fresh DB → create+stamp, existing pre-Alembic → stamp baseline+upgrade, normal → upgrade head). Generate with `make db-migrate msg="description"`.
- **Mixins**: `SourcedRecordMixin` in `app/mixins.py` adds `source_id`, `source_ts`, `synced_at`, `content_hash` to models pulled from external systems. Used by 8 of 28 tables.
- **OAuth**: Manual URL construction + httpx token exchange (NOT `google_auth_oauthlib.Flow`) to avoid PKCE auto-inject issues
- **Sync**: APScheduler CronTrigger per integration; 5-min timeout; sync state tracked in DB
- **Embeddings**: Unified pipeline — fastembed (BAAI/bge-small-en-v1.5, 384-dim) → pgvector. Shared `embeddings` + `embedding_queue` tables keyed by source (vault, gmail, whatsapp). Worker runs every 5 min. WhatsApp uses conversation-window chunking (30-min gap segmentation, runt merging, giant splitting).
- **Theme**: Dark mode only; CSS variables in `index.css`; cog-ui components from alexunism
- **Frontend**: Vite proxies `/api` to backend in dev. In production, FastAPI serves the built `frontend/dist/` as static files.

## Known Issues

- **The deploy host's disk filled twice from image pulls (2026-07-28 at 31 GB, 2026-09-02 at 61 GB), and Postgres crash-looped both times.** Every deploy pulls two images tagged `:latest` *and* `:<sha>`, so the previous ones stay tagged and are never dangling — a `prune -f` cron reclaims nothing from deploys. `make deploy-pull` now prunes unused images older than 24h after every `up -d` and prints `df -h /`; `system_alerts` has a host-disk axis at 85 %. Symptom to recognise: the app's health reads *connection failed / database system is in recovery mode*, which looks like networking and is a full disk. Check `df -h /` first.

- **Nothing arriving from the ingest webhook has a usable filename — never infer a type from one.** Tines (and a Shortcut posting directly) sends a bare UUID with **no extension**, and the route rewrites it anyway. This has now caused *four* separate bugs in four modules: `sniff_kind`'s ISO-BMFF check (iOS voice notes classified `unknown`, fixed 2026-07-31), `sniff_kind`'s HTML detection (saved pages previewed as their own doctype, fixed 2026-08-17), `vision/client.py::mime_for`, which read the suffix then `mimetypes.guess_type` then gave up — so **every** image was rejected as "not a Gemini-supported image format" and `vision` had never once succeeded in production until 2026-08-17 — and **`transcription/gemini.py::mime_for` (fixed 2026-08-29), which was the same function, with the same name, making the same mistake in the sibling package.** A real 10m53s memo arrived from a Shortcut named `Riverside 16` (a Shortcut's filename field is the memo's *title*) and was rejected as "not a Gemini-supported audio/video container" — while `scan.py` had already sniffed it as audio and read its duration off it. 🔑 **When you fix this bug, grep for the other `mime_for`.** The 2026-08-17 fix repaired `vision` and left its twin in `transcription` untouched for twelve days; the two packages cannot import each other (capability boundaries), so nothing but a grep will find the copy. Sniff bytes. Two ambiguities to respect: a WAV and a WebP share the `RIFF` prefix (the fourcc at 8:12 decides), and audio/video/stills all use `ftyp` (the brand at 8:12 decides; the first four bytes are a box *length*, never a constant).
- **A billable sweep that records its failures needs something surfacing the failure *rate*.** The vision bug above was invisible for twelve days because `described_at` is set even on failure — correct, since retrying costs money — so a *permanent* failure became a permanent skip, and a 100% failure rate presented as an empty queue. Two individually-correct decisions composing into silence. Retrying means clearing `described_at` on the sidecar by hand.
- **A config key's *type* cannot be changed without migrating the stored value.** `integration_config` holds whatever shape was written last, and `plugin_config()` validates with Pydantic on read — so changing `whatsapp_self_chat_jids` from `list_str` to `dict_str_str` left a stored `[]` that raised `ValidationError` on every read, which would have broken the whatsapp embedding cron every 30 minutes. There is no migration mechanism for config the way Alembic covers schema: write the new shape in the same change, and check it after deploying.
- **A queued write is not a completed write, and `reminder_commands` had never once been drained.** `commands.py`'s docstring promised a reaper as `TODO step 2.5`; it went unbuilt for five months, so every EventKit write made while the daemon's SSE subscription was dead was lost silently — **57 of the 114 `complete` commands ever issued** sat pending forever while callers were told `ok: true`. Fixed 2026-08-19: `dispatch_command` now returns `ok: False, applied: False` (the reporting was the more important half — `ok: true, queued: true` is indistinguishable from success at the call site, which is why the same incident was diagnosed in August, "fixed" with `launchctl kickstart -k`, and recurred), plus `drain_pending()` on SSE subscribe and a new `system_alerts` axis joining `stream_manager.connected_users()` to the pending count. ⚠️ **`drain_pending` is age-capped (`MAX_REPLAY_AGE`, 2h) and that is not a tuning knob** — see the next entry for why an uncapped reaper would have been destructive.
- **`backlog_sync` wrote command rows that nothing dispatched — 107,058 of them.** The vault→Reminders half built `ReminderCommand` rows and never called `dispatch_command`, so `stats["new_to_reminders"]` was counting writes into a queue with no reader; a task re-dated in the vault never reached Reminders and read as 18 days overdue. The volume came from one line: the dedupe guard used `payload.contains(task.text[:50])`, a **raw-text substring test against `json.dumps` output**. `json.dumps` escapes `"` to `\"` and (default `ensure_ascii`) `—` to `\u2014`, so any task with a quote or a non-ASCII character never matched itself and was re-queued every 30 minutes — measured on live rows, the test returned False for *all* of them including a pure-ASCII one. Fixed: compares the decoded `summary`, full text not a 50-char prefix, and the whole direction is gated behind `reminders_push_vault_to_reminders` (**default off**). 106,903 rows were marked `abandoned`, none executed.
- **Before thresholding a source, measure its cadence — twice now this was skipped.** `lastfm` alerted "data stale" on 13 and 19 August and was investigated as a comar fault both times; both times comar was correctly in sync (verified by calling the Last.fm API directly and getting the *identical* most-recent play). The 48h threshold sat **inside the normal distribution**: over 4,000 scrobbles there are 48 gaps of ≥24h, 4 of ≥48h, and a largest of exactly 72.0h, so it fired ~4×/year on a quiet weekend. Raised to 7d. ⚠️ Two traps here: `played_at` is when a track was *played*, not ingested, and **the scrobbler submits in batches** — on 19 Aug it flushed two days of plays at once, so a single point-in-time API check cannot distinguish "the source is dry" from "the source hasn't flushed yet". The alert text now says "no new data at the source", never "the source has stopped", because a batching upstream is not broken.
- **"I have no information" and "the thing is broken" keep rendering as the same alert.** Four instances found in two days, all defaulting to the alarming reading: `vision`'s permanent-failure-as-permanent-skip; the table-wide `func.max()` that let one live device mask a dead one (fixed `413328f`); `client_tokens.last_seen` being NULL for demonstrably-live devices; and `homeassistant`'s `ws_last_event_at()`, whose own docstring admits it returns None when the listener "hasn't started, **or** hasn't seen an event yet" — so **every deploy raises a spurious `homeassistant -> data stale (no records in table)`** until something in the house flips a non-numeric state. Unfixed as a class. The one place the distinction *is* deliberate is `facade.slept_hours()`, which returns `None` for "no session rows" and `0.0` for "recorded, all awake" — a deadline check that collapsed those would alert on the one night the export definitely worked.
- **`ha_entities` cannot attribute entity churn: no first-seen, and removals are hard-deleted.** HA went 1,573 → 1,717 entities in 24h (+46 offline) and the change was unrecoverable from stored data — `synced_at` is bumped on every sync (`row.synced_at = now`), so it is a heartbeat, and the only surviving artefact was a count. The cause was found by grouping the *current* offline population by device-name token: a **Dreame robot vacuum**, 289 entities, 184 `unavailable` (per-room `select`/`number` config entities that never populate while docked). `first_seen_at` added 2026-08-19 (migration `d5e2b8f1a9c4`) so the next delta is a query. Still open: removals leave no trace, and **`offline_count` is now permanently inflated by ~184 benign entities** — a single offline number is dead as a signal unless it excludes expected-unavailable or reports per-device.
- **Battery alerts must be split by what holds the battery.** A phone at 1% is ordinary life; a door sensor at 1% is a monitoring outage about to happen quietly. `low_battery` mixed both, so the actionable case arrived beside the routine one. Split 2026-08-19 into `low_battery` (hardware) and `personal_device_battery` (phones/watches/tablets), detected **structurally** — HA's companion app creates a sibling `<stem>_battery_state`/`_charger_type` sensor and nothing else does. Verified against the live registry: picks out exactly the three personal devices, leaves all thirteen hardware batteries alone. A name list would fail `tests/test_personalisation_guard.py` and need editing on every handset change.
- **HA returns 400 for transient conditions, but `homeassistant/client.py::call_service` classifies every 4xx as `PermanentError`.** Measured 2026-08-17: an identical title/message/`data` payload failed with 400 and then succeeded seconds later untouched, so nothing retried a push that would have worked. Matters now that inbox transcript/vision/ingest notifications all depend on pushes landing. Unfixed — deciding *which* 4xx are retryable needs its own change.
- **Adding an OAuth scope re-mints nothing — every existing token has to re-consent, and nothing tells you.** `google_docs` (2026-08-20) added `https://www.googleapis.com/auth/documents` to the union `app/auth/oauth.py::_oauth_scopes()` requests. That union is only applied at *consent* time, so tokens stored before that commit carry the old scope set and every `docs_*` tool 403s against them. `google_docs/client.py` classifies 401/403 as `PermanentError` precisely so this does not look like a retryable blip, but the tool still has to be *called* before anyone finds out — there is no boot check comparing each stored token's `scopes` column against the manifests that now need them, and `OAuthToken.scopes` is stored, so such a check is buildable and does not exist. Same trap as `sheets`' `spreadsheets`+`drive.file` addition on 2026-07-20; that one was discovered the same way. **After deploying an integration with a new scope, re-run consent for every account before assuming it works.**
- **`drive.file` is a per-file grant, which splits an integration's surface in half.** `google_docs` can whole-document overwrite only documents comar itself created (a Drive `files.update`), while read/append/replace reach anything the account can open (the Docs API, `documents` scope). So "I can edit this doc" and "I can rewrite this doc" are different questions with different answers for the *same* document, depending only on who created it. The asymmetry is deliberate — the alternative is `.../auth/drive`, full Drive access for every integration sharing the scope union — but it will read as a bug the first time a hand-made doc refuses a `docs_write`.
- **Postgres password gotcha**: The `pgdata` volume remembers the password from first init. If `HOME_DB_PASSWORD` changes in .env, run `bash scripts/init-db-password.sh` on the server to sync the password.
- **Google OAuth re-auth flow**: When a refresh token is revoked (Testing-mode 7-day expiry, user-side revocation, password change), `get_credentials` raises `NeedsReauthError`, sets `OAuthToken.needs_reauth_at`, and the scheduler **skips retry** for that integration's syncs until re-auth completes. The dashboard renders a banner with a one-click re-auth link; `system_alerts` returns the flagged tokens under `reauth_needed`. To recover: visit `/api/auth/google/login?account=<email>` (via comar.lab on LAN or the Tailscale URL off-LAN — both should be in Google Cloud Console's Authorized Redirect URIs). The structural fix to make this rare: publish the OAuth consent screen to **In Production** in Google Cloud Console (no verification needed for personal use under sensitive-scope thresholds) so the 7-day Testing-mode token clock goes away.
- **Last.fm backfill**: Now has per-request retry (3 attempts, 2/5/15s backoff) and resume cursor (persisted in SyncState). Backfill can resume from where it left off after failure.
- **Backlog sync**: Local fuzzy matching (difflib SequenceMatcher) — no API key needed. Runs every 30 min via scheduler + reactive trigger on PushReminders changes (instant sync on completions/additions/edits via background thread).
- **Google Calendar OAuth scope**: Upgraded from `calendar.readonly` to `calendar` (read/write) on 2026-04-01. All accounts need re-auth — now flagged automatically (see "Google OAuth re-auth flow" above).
- **Sheets/Drive OAuth scope**: Added `spreadsheets` + `drive.file` on 2026-07-20 for the snags-register Sheets export. Same re-auth mechanism as the calendar bump — the account named in `HOME_SHEETS_OWNER_ACCOUNT` must re-auth via `/api/auth/google/login?account=<email>` once before any export can create/write a sheet (`ensure_export` returns `None` silently until then, it doesn't raise).
