# comar-server

Data engine for the Comar (Co-Managed Archive) family knowledge system. Caches data from external APIs in Postgres, exposes tools over HTTP+SSE (`/api/v1/*`) for the comar-client and any other surface, and serves a web dashboard. Docker-deployed to a home server.

For deep internals (DSL tools, scheduler, model conventions, gotchas), see [server/CLAUDE.md](CLAUDE.md).

## Quick start

### 1. Environment

```bash
cp .env.example .env
```

Edit `.env` — at minimum:

```bash
HOME_DB_PASSWORD=<strong-password>
HOME_UI_TOKEN=<dashboard-access-token>
# HOME_MCP_TOKEN is legacy/dev-only — per-user bearers in client_tokens
# table are the live MCP auth path. Only set this if you need an
# admin/dev fallback bearer.
```

Generate tokens:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Start

```bash
docker compose up -d
```

This starts the FastAPI app (host port 8400, container port 8000) and depends on Postgres being available on the Docker network. See [Database](#database) for both options.

### 3. Create a client token

Each Mac authenticates with a per-device bearer token:

```bash
docker exec -it comar-app python -c "
from app.db import engine
from sqlalchemy import text
import secrets
token = secrets.token_urlsafe(32)
with engine.begin() as conn:
    conn.execute(text(
        \"INSERT INTO client_tokens (label, token_hash, user_name) VALUES (:label, :hash, :user)\"
    ), {'label': 'macbook', 'hash': token, 'user': 'alex'})
print(f'Token: {token}')
"
```

Hand the printed token to `comar setup` on the client.

### 4. Verify

```bash
# Dashboard
curl http://localhost:8400/api/health

# V3 client API (with bearer token)
curl -H "Authorization: Bearer <token>" http://localhost:8400/api/v1/heartbeat
```

## Database

Postgres 16 with the pgvector extension. Two options:

### Option A: External Postgres (recommended for shared infra)

If you run a shared Postgres instance (e.g. via a separate infra project), configure the connection in `.env`:

```bash
HOME_DATABASE__URL=postgresql://homeservices:<password>@home-services-db:5432/home_services
```

The `docker-compose.yml` connects to an external Docker network (`homelab_default`) where the database container lives. Adjust the network name and database hostname to match your setup.

### Option B: Standalone Postgres

Add a database service to `docker-compose.yml`:

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: homeservices
      POSTGRES_PASSWORD: ${HOME_DB_PASSWORD}
      POSTGRES_DB: home_services
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U homeservices"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  pgdata:
```

And update the `app` service to depend on it:

```yaml
  app:
    depends_on:
      db:
        condition: service_healthy
    environment:
      HOME_DATABASE__URL: postgresql://homeservices:${HOME_DB_PASSWORD}@db:5432/home_services
```

### Database password gotcha

The Postgres `pgdata` volume remembers the password from first init. If `HOME_DB_PASSWORD` changes in `.env`, you must also update it inside Postgres:

```bash
docker exec -it <postgres-container> psql -U homeservices -c "ALTER USER homeservices WITH PASSWORD 'new-password';"
```

## Integrations

### Google Calendar + Gmail

Requires a Google Cloud project with Calendar API and Gmail API enabled.

1. Create OAuth credentials (Desktop app type) in [Google Cloud Console](https://console.cloud.google.com/)
2. Add to `.env`:
   ```bash
   HOME_GOOGLE_CLIENT_ID=<your-client-id>
   HOME_GOOGLE_CLIENT_SECRET=<your-client-secret>
   ```
3. Authenticate via SSH tunnel (Google rejects private IPs as OAuth redirect URIs):
   ```bash
   # On your Mac:
   ssh -L 8400:localhost:8400 <server-host>
   # Then in the browser:
   # http://localhost:8400/api/auth/google/login?account=you@gmail.com
   ```
4. Add `http://localhost:8400/api/auth/google/callback` as an authorised redirect URI in Google Cloud Console

**Multi-account**: repeat for each Google account. Calendar visibility is configured in `backend/app/config.py` → `calendar_visibility` (per-account `"full"`, `"busy"`, or `"hidden"`).

### Apple Reminders

No server-side configuration. Reminders are read/written locally on each Mac via EventKit (PyObjC) and pushed to the server through `POST /api/v1/reminders/push`. The server stores a cache for the dashboard.

### Finance

CSV import from supported banks (AIB, Revolut). Drop CSVs in `csv-inbox/` and use the `import_finance` MCP prompt or call `finance_import_csv` directly.

### Obsidian Vault

Mount your vault tree into the container:

```bash
# .env
HOME_VAULTS_HOST_PATH=/path/to/your/vaults
```

The host path should contain a per-user subfolder for each Mac (e.g. `/path/to/your/vaults/alex/`) — `docker-compose.yml` mounts the whole tree at `/vaults` and additionally bind-mounts `<host-path>/alex/` to `/obsidian` as a back-compat alias. A watchdog process monitors for changes and re-indexes files for semantic search. The client also pushes individual file changes via `POST /api/v1/vault/push` for immediate re-indexing.

**Sync**: the vault is synced to the server independently (Google Drive + rclone, iCloud, Syncthing, etc.).

### WhatsApp

A Node.js sidecar container using [Baileys](https://github.com/WhiskeySockets/Baileys) (unofficial WhatsApp Web protocol). Read-only — no messages are sent.

First-time setup requires QR pairing:

```bash
docker compose logs -f whatsapp-bridge
# Scan the QR code with WhatsApp on your phone
```

Set `HOME_WA_SYNC_HISTORY=true` in `.env` for a one-time history backfill (can take hours for large histories). Reset to `false` after.

### Last.fm

```bash
# .env
HOME_LASTFM_API_KEY=<your-api-key>
HOME_LASTFM_USERNAME=<your-username>
```

Get an API key from [Last.fm](https://www.last.fm/api/account/create).

Backfill historical scrobbles:

```bash
curl -X POST http://localhost:8400/api/integrations/lastfm/backfill
```

### Weather

Uses [Open-Meteo](https://open-meteo.com/) (free, no API key). Location is set via `HOME_WEATHER_LATITUDE` / `HOME_WEATHER_LONGITUDE` in `.env`, with a sensible default baked in (Dublin: `53.3498` / `-6.2603`).

### Irish Rail

Uses the [Irish Rail API](http://api.irishrail.ie/realtime/) (free, no API key). Station is set via `HOME_RAIL_STATION_CODE` / `HOME_RAIL_STATION_NAME` in `.env`, with a sensible default baked in (Malahide: `MHIDE`).

### Coffee, Attachments, Apple Health, Historical Corpus

See `server/CLAUDE.md` for the full integration table — schedules, tool counts, and data sources for all 18 integrations.

## Architecture

```
comar-app container
├── FastAPI (host port 8400)
│   ├── /api/v1/* — V3 client API (bearer-token auth):
│   │     heartbeat, tools, tools/{name}, vault/push,
│   │     reminders/push, health/push, logs/push, events (SSE),
│   │     prompts, instructions
│   ├── /api/* — REST endpoints (dashboard, auth, integrations, client dist — UI cookie auth)
│   ├── /mcp/sse — MCP server (direct access for Claude Desktop / other surfaces)
│   └── /* — static frontend (React build)
│
├── APScheduler (background sync per integration)
├── Embedding worker (every 5 min)
│   ├── fastembed (BAAI/bge-small-en-v1.5, 384-dim)
│   ├── Unified queue (vault, email, WhatsApp)
│   └── Stores in pgvector
│
└── Vault watcher (watchdog on /obsidian, debounced re-indexing)
```

## Integration pattern

Every integration lives in `backend/app/integrations/<name>/` and follows the `BaseIntegration` ABC:

| File | Purpose |
|------|---------|
| `__init__.py` | Class implementing `BaseIntegration` |
| `client.py` | External API access |
| `models.py` | SQLAlchemy models |
| `sync.py` | Data sync logic |
| `tools.py` | MCP tool definitions |

Required methods: `name` / `display_name`, `sync()`, `mcp_tools()`, `dashboard_data()`, `sync_schedule()`, `is_configured()`.

Mechanical tools (list, search, semantic search, stats) use the declarative DSL in `app/tools/`. Domain-specific and admin tools stay hand-written, wrapped in `CustomTool`. Every tool carries MCP annotations from `app/mcp/annotations.py`.

### Adding a new integration

1. Create `backend/app/integrations/<name>/` with the files above
2. Implement `BaseIntegration`
3. **Import all models** in `app/models/__init__.py` (critical — `create_tables()` won't see them otherwise)
4. Register in `integrations/__init__.py` → `register_all()`
5. Add config fields to `HomeSettings` in `config.py`
6. MCP tools and scheduler auto-discover from there

## Deployment

### Commands (from `server/Makefile`)

```bash
make dev              # Local: uvicorn --reload + vite dev (two terminals)
make deploy-pull      # DEFAULT: pull pre-built GHCR images + restart (after CI push)
make deploy-build     # Dev rsync path: build frontend + rsync + docker compose up --build
make deploy-fast      # Dev rsync path, backend only: rsync + docker compose up --build app
make server-logs      # Tail app container logs
make server-status    # Show container status
make server-restart   # Restart app container
make server-down      # Stop everything
make server-init      # First-time: create dir on server, copy .env template
```

### How deploy works

**CI deploy** (`make deploy-pull` — the default):
1. Push to `main` triggers GitHub Actions
2. CI builds frontend, client wheel, and Docker images
3. Pushes to GHCR (`ghcr.io/cograda/comar-oss-app`, `ghcr.io/cograda/comar-oss-whatsapp`)
4. Run `make deploy-pull` on the server to pull and restart

**Dev rsync deploy** (`make deploy-build`, or `deploy-fast` for backend-only) — use only to test uncommitted changes on the box without going through CI:
1. Builds the React frontend (`npm run build`) — skipped by `deploy-fast`
2. Rsyncs project files to the server
3. SSH → `docker compose up -d --build`

### Reverse proxy

The HTTP API is LAN-only on port 8400. A reverse proxy (Caddy in this setup) terminates HTTPS for the web dashboard at `https://comar.lab`. There is no separate gRPC port — everything goes through the same FastAPI app.

Example Caddy config (using Docker labels):

```
caddy: comar.lab
caddy.reverse_proxy: "{{upstreams 8000}}"
caddy.tls: internal
```

## Environment variables

All prefixed `HOME_`. See `.env.example` for the full template.

| Variable | Required | Purpose |
|----------|----------|---------|
| `HOME_DATABASE__URL` | Yes | Postgres connection string |
| `HOME_DB_USER` | Yes | Postgres user (for docker-compose) |
| `HOME_DB_PASSWORD` | Yes | Postgres password |
| `HOME_UI_TOKEN` | Yes | Web dashboard access token |
| `HOME_MCP_TOKEN` | Legacy | Pre-multi-user MCP bearer (dev/admin fallback). Per-user `client_tokens` is the live auth path. |
| `HOME_PORT` | No | Host port mapping (default 8400) |
| `HOME_GOOGLE_CLIENT_ID` | For OAuth | Google Calendar + Gmail |
| `HOME_GOOGLE_CLIENT_SECRET` | For OAuth | Google Calendar + Gmail |
| `HOME_OAUTH_REDIRECT_BASE` | For OAuth | Set to `http://localhost:8400` for SSH tunnel auth |
| `HOME_LASTFM_API_KEY` | For Last.fm | Last.fm API key |
| `HOME_LASTFM_USERNAME` | For Last.fm | Last.fm username |
| `HOME_OBSIDIAN_VAULT_PATH` | For vault | Path inside container (default `/obsidian`) |
| `HOME_VAULTS_HOST_PATH` | Docker | Host path to the vault tree (should contain a per-user subfolder, e.g. `<host-path>/alex/`) |
| `HOME_ANTHROPIC_API_KEY` | For backlog sync | Anthropic API key (Haiku task matching) |
| `HOME_WA_SYNC_HISTORY` | No | `true` for one-time WhatsApp backfill |
| `HOME_OAUTH_ENCRYPTION_KEY` | Recommended | Fernet key for OAuth token encryption at rest |
| `HOME_WEATHER_LATITUDE` / `HOME_WEATHER_LONGITUDE` | No | Weather forecast location (default: Dublin) |
| `HOME_RAIL_STATION_CODE` / `HOME_RAIL_STATION_NAME` | No | Irish Rail home station (default: Malahide) |
| `HOME_TRANSFER_MATCH_NAMES` | No | Comma-separated account-holder names for internal transfer detection |

## Web dashboard

React 19 SPA served from the app container. Access via the reverse proxy (`https://comar.lab`) or directly at `http://<server-ip>:8400`. Login with `HOME_UI_TOKEN`.

Features:
- Integration status and manual sync triggers
- Calendar events and reminders overview
- Finance summary
- Google account management (OAuth tokens)

## Client distribution

The server serves client wheels for auto-updates:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/client/version` | Latest version + wheel filename |
| `GET /api/client/download/latest` | Download latest wheel |
| `GET /api/client/download/<filename>` | Download specific wheel |
| `GET /api/client/bootstrap.sh` | Bootstrap script for first install |

Place built wheels in `server/client-dist/`. CI does this automatically.

## Development

### Local dev

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

### Tests

```bash
cd backend
python -m pytest tests/ -v
```

### coglib dependency

The server uses `coglib` (shared config, DB, logging), vendored in `server/coglib/`. If you maintain coglib in a separate upstream repo, refresh the vendored copy with:

```bash
make refresh-coglib
```
