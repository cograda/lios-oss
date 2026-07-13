# comar-server

Data engine for the comar (Co-Managed Archive) family knowledge system. Caches data from external APIs in Postgres, exposes tools over MCP (Streamable HTTP at `/mcp/`) and a smaller HTTP+SSE control plane (`/api/v1/*`) for the local daemon, and serves a web dashboard. Docker-deployed to a home server.

## Tech Stack

- **Backend**: Python 3.12, FastAPI, coglib (shared config + DB + logging, vendored in `server/coglib/`), SQLAlchemy 2.0, Alembic (schema migrations), Pydantic 2.0
- **Frontend**: React 19, Vite 6, Tailwind v4, React Router 7, TypeScript 5
- **UI**: Components from cog-ui (shared UI library), dark-mode-first theme
- **Database**: Postgres 16 with pgvector extension (for embedding search)
- **Embeddings**: fastembed (BAAI/bge-small-en-v1.5, 384-dim) — unified pipeline (vault, Gmail, WhatsApp)
- **WhatsApp Bridge**: Node.js sidecar container (Baileys @whiskeysockets/baileys), writes to shared Postgres
- **HTTP API (V3)**: `/api/v1/*` on FastAPI port 8400 — tools, push (vault/reminders/health/logs), events (SSE), heartbeat, prompts, instructions. Bearer-token auth via `client_tokens`. **No gRPC, no protobuf.**
- **MCP**: Python `mcp` SDK; **stateless Streamable HTTP** transport at `/mcp/`, per-user bearer auth. Claude Code (and any other MCP client) connects directly — this is the primary tool path. The local daemon still boots a legacy localhost MCP proxy (retirement pending, separate task); its main jobs are EventKit + vault watcher side-car duties.
- **Scheduling**: APScheduler (AsyncIOScheduler) for background sync jobs
- **Auth**: Bearer token cookie for web UI (`HOME_UI_TOKEN`), MCP/SSE bearer (`HOME_MCP_TOKEN`), per-user bearer for HTTP API (`client_tokens` table), Google OAuth for calendar/Gmail

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

comar-client (on each Mac, side-car — primary path is server /mcp/;
              daemon still runs a legacy localhost MCP proxy pending retirement)
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
├── coglib/                    # Synced to server for Docker build
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
│   │   ├── test_tool_snapshots.py    # Golden-output suite (48 tools, 11 integrations);
│   │   │                             #   goldens in tests/snapshots/ (gitignore-negated),
│   │   │                             #   regenerate ONLY deliberately via UPDATE_SNAPSHOTS=1
│   │   ├── test_tool_calls.py        # tool_calls persistence + system_alerts tool checks
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
│       │   └── clients.py     # ClientToken (HTTP/MCP device auth), ClientLog (remote log shipping)
│       ├── routes/
│       │   ├── __init__.py    # /api prefix, mounts all routers
│       │   ├── auth.py        # Login, check, Google OAuth callback, token list
│       │   ├── dashboard.py   # GET /api/dashboard/summary (aggregates all integrations)
│       │   ├── integrations.py # List, sync trigger, backfill endpoints
│       │   └── client_dist.py # Client wheel download, version check, bootstrap script
│       ├── mcp/
│       │   └── server.py      # MCP server (Streamable HTTP), auto-discovers tools, asyncio.to_thread
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
└── scripts/
    └── reminders-sync/        # LEGACY — Mac-side osascript agent, superseded by
                                #   the client daemon's EventKit push (see "Why
                                #   EventKit instead of osascript?" in root README).
                                #   Not deployed; kept for reference only.
        ├── sync.py            # Reads via osascript, pushes to server, executes commands
        ├── run.sh             # Wrapper for launchd
        └── com.cograda.reminders-sync.plist  # launchd plist (reference copy)
```

## Integration Pattern

Every integration lives in `backend/app/integrations/<name>/` and follows the `BaseIntegration` ABC:

| File | Purpose |
|------|---------|
| `__init__.py` | Class implementing `BaseIntegration` — `name`, `display_name`, `sync()`, `mcp_tools()`, `dashboard_data()`, `sync_schedule()`, `is_configured()` |
| `client.py` | External API access (HTTP calls, SDK wrappers) |
| `models.py` | SQLAlchemy models (inherit `coglib.Base`) |
| `sync.py` | Data sync logic (called by scheduler or manually) |
| `tools.py` | MCP tool definitions with handler functions |

**Exceptions to the 5-file pattern:**
- `apple_reminders/` also has `commands.py` + `routes.py` — this is the LIVE server-enqueue → SSE → EventKit dispatch path (used by tools.py, api/v1.py, backlog_sync.py), not dead code — and `backlog_sync.py` (vault↔Reminders two-way sync via Haiku)
- `finance/` has `services.py` instead of `client.py` (no external API — CSV parsing, categorisation, analytics, transfer detection)
- `irish_rail/` has no `models.py` or `sync.py` (live API, no caching)
- `whatsapp/` has no `client.py` (bridge writes directly to DB; sync.py handles conversation-window chunking for embeddings)
- `system/` has only `__init__.py` + `tools.py` (no client, models, or sync — diagnostic tools that query cross-integration state)

### Adding an Integration

1. Create `backend/app/integrations/<name>/` with the files above
2. Implement `BaseIntegration` (see `integrations/base.py` for the ABC)
3. Import ALL models in `app/models/__init__.py` (or `create_tables()` won't create them)
4. Register in `integrations/__init__.py` → `register_all()`
5. Add any config fields to `HomeSettings` in `config.py`
6. MCP tools and scheduler auto-discover from there

### Current Integrations (18)

| Integration | Sync Schedule | MCP Tools | Data Source | Notes |
|-------------|--------------|-----------|-------------|-------|
| `google_calendar` | */15 * * * * | 4 | Google Calendar API (OAuth, read/write) | Multi-account (4+ personal + work), `create_event` tool |
| `google_mail` | */15 * * * * | 8 | Gmail API (OAuth) | ~10k messages, pgvector semantic search |
| `apple_reminders` | Push (EventKit, 30s) + reactive backlog sync | 3 | EventKit via PyObjC on client | `reminders_add`/`complete` on client. Reactive vault sync on changes. |
| `apple_health` | Push (no scheduled sync) | 7 | Health Auto Export iOS app → `/api/v1/health/push` (v3) or `/api/health/push` (legacy) | Daily metrics + workouts + sleep. SyncState bumped on push for freshness alerts. |
| `finance` | None (manual) | 8 | CSV import (AIB, Revolut) | ~4,700 txns, ~670 rules, ~98% coverage |
| `obsidian` | */30 * * * * | 3 | Local vault files | pgvector + fastembed, ~300 files |
| `whatsapp` | */30 * * * * | 7 | Baileys bridge (Node.js sidecar) | ~15k messages, conversation-window embedding |
| `weather` | */30 * * * * | 2 | Open-Meteo API (no key) | Dublin coords (configurable) |
| `lastfm` | */15 * * * * | 4 | Last.fm API | 50,000+ scrobbles |
| `irish_rail` | None (live) | 2 | Irish Rail XML API | Malahide station (configurable), no caching |
| `homeassistant` | WS events (real-time) + */5 poll reconcile | 4 | Home Assistant REST + WebSocket (192.168.1.51) | Gap-free: WS `state_changed` listener (lifespan task, `events.py`) feeds `ha_entities`/`ha_state_changes` live; the 5-min poll reconciles after downtime + refreshes areas. Numeric ticks excluded from history unless `HOME_HA_RECORD_NUMERIC_HISTORY`. Signal curation in `tools.py::SECTIONS`. |
| `system` | None | 4 | Cross-integration diagnostics | Health alerts, morning briefing, week ahead, search everything |
| `media` | 5,35 * * * * | 4 | Baileys bridge `/download/:id` | WhatsApp media store: indexes ALL images/videos/audio in `media_items`, auto-downloads last ~30 days to `HOME_MEDIA_ROOT` volume, `media_export` copies into the vault for note-embedding. Documents stay with `attachments`. **Host dir must live OUTSIDE `~/comar-server/`** (rsync deploy uses `--delete`). |
| `snags` | None (user-gated capture) | 5 | WhatsApp `Snag - …` messages + manual | Snag register — **DB is source of truth**, immutable UIDs (`SNAG-0042`), trade/severity/status lifecycle, evidence via `snag_media` → media store. Vault note `Household/Renovation/Snags.md` is a generated one-way view (re-rendered on every write; evidence auto-exported to `Attachments/Snags/UID-n.jpg`). |
| `coffee` | None (manual entry) | 12 | User-entered via MCP tools (`coffee_log`, `coffee_brew`) | Brew log + bean dial-in advisor, ported from brewhaha |
| `attachments` | */30 * * * * (piggybacks on WhatsApp cadence) | 4 | WhatsApp message attachments (Gmail scanning not yet implemented) | User-gated flow: `scan` → `pending` → `ingest`; ingested files land in `historical_documents` alongside the corpus |
| `historical_corpus` | None (one-shot CLI/REST ingest) | 2 | Static renovation document archive | No scheduled sync — ingested manually via `scripts/ingest_historical_corpus.py` or the REST endpoint; queried via `renovation_context` |
| `inbox` | 7 * * * * | 6 | `/inbox/<bucket>/` webhook drop zone (automation ingestion) | Triage layer on top of the drop zone: hourly enrichment (kind sniff + preview), then `pending`/`preview`/`archive`/`dismiss`/`to_vault`/`to_corpus` |

## MCP Server

Stateless **Streamable HTTP** transport at `/mcp/`. Per-user bearer auth via the `client_tokens` table (`HOME_MCP_TOKEN` is legacy/dev-only — see Auth section).

Tools are auto-discovered from each integration's `mcp_tools()` method at startup. Tool handlers run in `asyncio.to_thread()` to avoid blocking the event loop. Naming convention: `integration_action` (e.g. `calendar_today`, `finance_summary`). Per-user request scoping is pinned via `current_user_id()` (ContextVar) for the duration of each handler.

~80 tools across the integrations below. See the vault `CLAUDE.md` for the full tool reference table.

## Scheduler

APScheduler `AsyncIOScheduler` registered in `scheduler.py`. Each integration with a `sync_schedule()` gets a cron job.

- **Timeout**: 5 minutes per sync (`asyncio.wait_for`)
- **Overlap prevention**: `max_instances=1` per integration
- **Misfire handling**: `misfire_grace_time=120` (skips if >2 min late)
- **State tracking**: Writes to `SyncState` table after every sync (ok/error/timeout) — visible via `/api/integrations/`

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
| `vault_chunks` | obsidian | Vault file hashes + modification times (incremental indexing tracker) |
| `weather_current` | weather | Current conditions (single row, replaced each sync) |
| `weather_forecasts` | weather | 7-day daily forecast (upsert by date) |
| `scrobbles` | lastfm | Music listening history |
| `ha_entities` | homeassistant | Latest state per HA entity (upsert each 5-min sync) |
| `ha_state_changes` | homeassistant | Append-only non-numeric state transitions (appliance cycles, switches, presence) |
| `media_items` | media | WhatsApp media store index (status, storage_path, sha256; UserOwnedMixin) |
| `snags` + `snag_media` + `snag_source_messages` | snags | Snag register (household-shared): UID from `snag_uid_seq`, evidence links, idempotent capture tracking |
| `whatsapp_messages` | whatsapp | Messages captured by bridge (chat_id, sender, body, media) |
| `whatsapp_contacts` | whatsapp | Contacts and groups with last message times |
| `embeddings` | Core | Unified embedding store (pgvector 384-dim, source-keyed) |
| `embedding_queue` | Core | Pending items for embedding worker (source, status, text) |

### Critical: Model Registration

ALL SQLAlchemy model classes MUST be imported in `app/models/__init__.py`. If a model isn't imported there, `create_tables()` won't see it and the table won't be created on startup. This is the #1 gotcha when adding new integrations.

### Multi-user pattern (UserOwnedMixin)

Per-user tables apply `UserOwnedMixin` from `app/mixins.py` — one column `user_id INT NOT NULL FK → users.id ON DELETE RESTRICT, indexed`. Composite uniques start with `user_id` (e.g. `(user_id, uid)`).

Tables that have it: `client_tokens`, `client_logs`, `oauth_tokens`, `reminders` (+`account_email` for EventKit multi-account routing), `reminder_commands`, `mail_messages`, `scrobbles`, `whatsapp_messages`, `coffee_brews`, `message_attachments`, `health_daily_metrics`, `health_workouts`, `health_sleep_sessions`.

Shared / household-scoped tables deliberately don't take the mixin: finance (joint), `vault_chunks`, `historical_documents`, `weather_*`, `artist_tags` (community metadata), `coffees` (the bag, shared), `coffee_equipment_profiles`, `whatsapp_contacts` (global graph).

When adding a new table: per-user is the safer default.

## Authentication

### Web UI
Cookie-based bearer token (`HOME_UI_TOKEN`). Set on login, checked by middleware on all `/api/*` routes. 30-day expiry.

### MCP
Header-based bearer token (`HOME_MCP_TOKEN`). Checked in `verify_bearer_token()` before SSE connection. If not set, MCP endpoint is unauthenticated (dev mode).

### Per-user bearer (V3 client API)
Each daemon Mac gets its own row in `client_tokens` (FK → `users.id`). `app/auth/client_token.py::get_current_user` validates the bearer, joins client_tokens → users, and returns the **detached `User` model** (not just a string). Snapshot attrs before commit — `expire_on_commit=True` will bite anyone who tries to read user attrs after the session closes.

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
| `/api/integrations/` | GET | List integrations with sync status |
| `/api/integrations/{name}/sync` | POST | Trigger manual sync |
| `/api/auth/tokens` | GET | List connected OAuth accounts |
| `/api/auth/google/login?account=email` | GET | Start Google OAuth flow |
| `/api/auth/google/callback` | GET | OAuth callback (exchanged code for tokens) |
| `/api/health` | GET | Health check |

### Apple Reminders (Mac agent)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/reminders/sync` | POST | Push full reminder state from Mac |
| `/api/reminders/backlog-sync` | POST | Trigger vault backlog ↔ Reminders sync |

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
make refresh-coglib   # Sync vendored coglib from its upstream repo
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
2. CI builds frontend, builds Docker images, pushes to `ghcr.io/cograda/comar-oss-app` and `ghcr.io/cograda/comar-oss-whatsapp`
3. `make deploy-pull` → SSH into server, `docker compose pull && docker compose up -d`

**Dev rsync deploy** (`make deploy-build`, or `deploy-fast` for backend-only) — use only to test uncommitted changes on the box without going through CI:
1. `make build` — runs `npm run build` in `frontend/`, outputs to `frontend/dist/` (skipped by `deploy-fast`)
2. `make sync` — rsyncs project + proto stubs to `ubuntu:~/comar-server/`
3. SSH into server, `docker compose up -d --build` — rebuilds the app image and restarts

coglib is vendored in `server/coglib/` (copied from its upstream repo). Refresh with `make refresh-coglib` — it also stamps `server/coglib/.vendored-commit` (upstream HEAD sha + ISO date); CI warns if the stamp is missing or >90 days old.

### Docker architecture

- **db** container: `pgvector/pgvector:pg16`, persistent volume `pgdata`, healthcheck via `pg_isready`
- **app** container: Python 3.12-slim, installs coglib + requirements, copies backend + frontend/dist
- Port mapping: container port 8000 → host port 8400 (configurable via `HOME_PORT`)
- Vault mounted at `/obsidian` inside container (from `HOME_VAULTS_HOST_PATH` on host)
- Fastembed model cache persisted in `fastembed_cache` volume

### Server details

- **Host**: `ssh ubuntu` (`ubuntu` is your server's SSH alias; SERVER_IP, Proxmox VM)
- **Path**: `~/comar-server/`
- **HTTP API**: `http://SERVER_IP:8400` (LAN, no TLS), `https://comar.lab` (LAN via Caddy + internal CA), or `https://your-server.your-tailnet.ts.net` (anywhere via Tailscale, browser-trusted LE cert)
- **Dashboard**: `https://comar.lab` (Caddy proxy from infra project)
- **Remote MCP**: Off-LAN clients use the Tailscale URL — same `/mcp/` path, same per-user bearer. Routing for that hostname is a static block in `infra/Caddyfile`; cert is host-managed at `/opt/homelab/certs` and renewed weekly. See infra/CLAUDE.md.
- **Vault on server**: `/home/comar/vaults/` — per-user tree (`/home/comar/vaults/alex/`, and `/home/comar/vaults/sam/` once Sam onboards a second vault). Single-user vaults: the cross-user `/home/comar/vaults/shared/` tree and the `Shared/` logical namespace were retired 2026-06-05. Mounted into the app container at `/vaults`; `/home/comar/vaults/alex/` is also bind-mounted to `/obsidian` as a back-compat alias for callers not yet migrated to `app.services.vault_paths.resolve()`. Synced bidirectionally with each Mac via Syncthing (the server runs Syncthing on the host, paired per-device via `POST /api/v1/syncthing/pair`). The historical rclone+Google-Drive sync is retired.

## Environment Variables

All prefixed `HOME_`. Stored in `.env` on the server (never committed).

| Variable | Required | Purpose |
|----------|----------|---------|
| `HOME_DATABASE__URL` | Yes | Postgres connection (overridden by docker-compose for container networking) |
| `HOME_DB_USER` | Yes | Postgres user (used by docker-compose) |
| `HOME_DB_PASSWORD` | Yes | Postgres password (used by docker-compose) |
| `HOME_UI_TOKEN` | Yes | Web dashboard access token |
| `HOME_MCP_TOKEN` | Legacy | Pre-multi-user MCP bearer. Per-user `client_tokens` is the live path; this is dev-only fallback. |
| `HOME_GOOGLE_CLIENT_ID` | For OAuth | Google OAuth client ID |
| `HOME_GOOGLE_CLIENT_SECRET` | For OAuth | Google OAuth client secret |
| `HOME_OAUTH_REDIRECT_BASE` | For OAuth | Set to `http://localhost:8400` for SSH tunnel auth |
| `HOME_OAUTH_ISSUER` | For claude.ai connector | Public issuer URL for the MCP OAuth 2.1 authorization server (e.g. the Tailscale host). Empty disables the OAuth routes. |
| `HOME_LASTFM_API_KEY` | For Last.fm | Last.fm API key |
| `HOME_LASTFM_USERNAME` | For Last.fm | Last.fm username (your_username) |
| `HOME_OBSIDIAN_VAULT_PATH` | For vault | Legacy in-container alias path, bind-mounted to Alex's vault (default `/obsidian`) |
| `HOME_VAULTS_ROOT_PATH` | For vault | Root of the per-user vault tree inside the container (default `/vaults`) |
| `HOME_VAULTS_HOST_PATH` | Docker | Host path to the vault tree, mounted into the container at `/vaults`. Should contain a per-user subfolder matching each Mac's user (e.g. `<host-path>/alex/`, bind-mounted to `/obsidian` per docker-compose.yml). |
| `HOME_ANTHROPIC_API_KEY` | For backlog sync | Anthropic API key (Haiku task matching) |
| `HOME_WA_SYNC_HISTORY` | No | Set `true` for one-time WhatsApp history backfill (default false) |
| `HOME_HA_URL` | For HA | Home Assistant URL (e.g. `http://192.168.1.51:8123`) |
| `HOME_HA_TOKEN` | For HA | Home Assistant long-lived access token (dedicated "comar" token) |
| `HOME_MEDIA_HOST_PATH` | Docker | Host dir for the WhatsApp media store (mounted at `/data/media`) |
| `HOME_HA_RECORD_NUMERIC_HISTORY` | No | Also record numeric→numeric transitions in `ha_state_changes` (default false; HA's recorder keeps numeric series) |
| `HOME_WEATHER_LATITUDE` / `HOME_WEATHER_LONGITUDE` | No | Weather forecast location for the Open-Meteo integration (default: Dublin, `53.3498` / `-6.2603`) |
| `HOME_RAIL_STATION_CODE` / `HOME_RAIL_STATION_NAME` | No | Irish Rail home station code + display name (default: Malahide, `MHIDE`) |
| `HOME_TRANSFER_MATCH_NAMES` | No | Comma-separated account-holder names as they appear on bank statements — used to classify Revolut transfers between your own accounts as internal |
| `HOME_INBOX_PATH` | No | Path inside the container for the automation webhook ingestion pipeline (default `/inbox`) |
| `HOME_INBOX_TOKEN` | No | Bearer token for the `/api/inbox/ingest` webhook |
| `HOME_HEALTH_PUSH_TOKEN` | No | Separate bearer token for Health Auto Export pushes (least-privilege, distinct from `HOME_UI_TOKEN`) |
| `HOME_SYNCTHING_URL` | No | Server-side Syncthing REST URL, reached over the docker bridge gateway (default `http://172.21.0.1:8384`) |
| `HOME_SYNCTHING_API_KEY` | No | Syncthing REST API key (generated by Syncthing on first run, read from its `config.xml`) |
| `HOME_SYNCTHING_FOLDER_ID` | No | Folder ID shared with every paired Mac (default `vault`) — cross-device contract, don't rename without updating each client |
| `HOME_CALENDAR_VISIBILITY` | No | Per-account calendar visibility map (`"full"` / `"busy"` / `"hidden"`); unrecognised accounts default to `"full"` |

Client config lives in `~/.config/comar/config.toml` on each Mac (created by `comar setup`). Client tokens are stored in the `client_tokens` table — create via server admin or CLI.

## Conventions

- **Config**: `HomeSettings(CogSettings)` with `HOME_` env prefix, `__` nested delimiter (e.g. `HOME_DATABASE__URL`)
- **Database**: coglib pattern — `coglib.Base` for models, `db.session()` for sessions, `SessionDep` for FastAPI dependency injection
- **MCP tools**: Namespaced `integration_action` (e.g. `calendar_list_events`). Handler functions take `(session, arguments)` and return JSON strings. Mechanical tools (list, search, semantic search, stats) use the declarative DSL in `app/tools/`. Domain-specific and admin tools stay hand-written, wrapped in `CustomTool`.
- **Migrations**: Alembic for schema changes. `db.py` auto-runs migrations on startup (three-way: fresh DB → create+stamp, existing pre-Alembic → stamp baseline+upgrade, normal → upgrade head). Generate with `make db-migrate msg="description"`.
- **Mixins**: `SourcedRecordMixin` in `app/mixins.py` adds `source_id`, `source_ts`, `synced_at`, `content_hash` to models pulled from external systems. Used by 8 of 28 tables.
- **OAuth**: Manual URL construction + httpx token exchange (NOT `google_auth_oauthlib.Flow`) to avoid PKCE auto-inject issues
- **Sync**: APScheduler CronTrigger per integration; 5-min timeout; sync state tracked in DB
- **Embeddings**: Unified pipeline — fastembed (BAAI/bge-small-en-v1.5, 384-dim) → pgvector. Shared `embeddings` + `embedding_queue` tables keyed by source (vault, gmail, whatsapp). Worker runs every 5 min. WhatsApp uses conversation-window chunking (30-min gap segmentation, runt merging, giant splitting).
- **Theme**: Dark mode only; CSS variables in `index.css`; cog-ui components (shared UI library)
- **Frontend**: Vite proxies `/api` to backend in dev. In production, FastAPI serves the built `frontend/dist/` as static files.

## Known Issues

- **Postgres password gotcha**: The `pgdata` volume remembers the password from first init. If `HOME_DB_PASSWORD` changes in .env, run `bash scripts/init-db-password.sh` on the server to sync the password.
- **Google OAuth re-auth flow**: When a refresh token is revoked (Testing-mode 7-day expiry, user-side revocation, password change), `get_credentials` raises `NeedsReauthError`, sets `OAuthToken.needs_reauth_at`, and the scheduler **skips retry** for that integration's syncs until re-auth completes. The dashboard renders a banner with a one-click re-auth link; `system_alerts` returns the flagged tokens under `reauth_needed`. To recover: visit `/api/auth/google/login?account=<email>` (via comar.lab on LAN or the Tailscale URL off-LAN — both should be in Google Cloud Console's Authorized Redirect URIs). The structural fix to make this rare: publish the OAuth consent screen to **In Production** in Google Cloud Console (no verification needed for personal use under sensitive-scope thresholds) so the 7-day Testing-mode token clock goes away.
- **Last.fm backfill**: Now has per-request retry (3 attempts, 2/5/15s backoff) and resume cursor (persisted in SyncState). Backfill can resume from where it left off after failure.
- **Backlog sync**: Local fuzzy matching (difflib SequenceMatcher) — no API key needed. Runs every 30 min via scheduler + reactive trigger on PushReminders changes (instant sync on completions/additions/edits via background thread).
- **Google Calendar OAuth scope**: Upgraded from `calendar.readonly` to `calendar` (read/write) on 2026-04-01. All accounts need re-auth — now flagged automatically (see "Google OAuth re-auth flow" above).
