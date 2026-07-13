# Comar

**Co-Managed Archive** — from the Irish *comar*, meaning a confluence or meeting place.

A family knowledge system built around an [Obsidian](https://obsidian.md) vault. Comar bridges your personal data — calendar, email, reminders, finances, health, music, WhatsApp, weather, coffee — into a single MCP endpoint accessible through Claude Desktop, Claude Code, or any MCP-compatible client.

## How it works

```
Claude Code ── MCP / Streamable HTTP (per-user bearer, COMAR_TOKEN) ── comar-server (home server, Docker)
                                                                            ├── ~13 integrations, ~80 tools
                                                                            ├── Postgres 16 + pgvector (semantic search)
                                                                            ├── WhatsApp bridge (Baileys sidecar)
                                                                            ├── APScheduler (background sync)
                                                                            └── Web dashboard (React 19)

comar-client (macOS daemon, side-car — not on the Claude Code MCP path)
    ├── Vault watcher: fsevents → POST /api/v1/vault/push
    ├── Apple Reminders: EventKit (PyObjC), dispatched via server SSE callbacks
    └── Legacy local MCP proxy on localhost:9400 — used only by Claude Desktop
        via mcp-remote (see "Connect Claude" below), pending retirement
```

Claude Code talks directly to the server's `/mcp/` endpoint over Streamable HTTP, authenticated with a per-user bearer token (`COMAR_TOKEN`) — this is the primary, supported path and doesn't go through the daemon at all. The daemon runs on each Mac for two jobs the server can't do itself: writing to Apple Reminders via EventKit, and watching the vault folder for changes to push to the server for re-indexing. It also still boots a legacy local MCP proxy that Claude Desktop connects to via `mcp-remote` (Claude Desktop doesn't yet support connecting to a remote HTTP MCP server directly) — that proxy has nothing to do with the Claude Code path and is on its own deprecation track. Transport between client and server is HTTPS + JSON, with SSE for server→client signals. **No gRPC, no protobuf.**

Audio transcription has moved out of comar-client into a separate repo (`cograda/scribe`), which drops markdown into a watched folder.

## What you get

| Integration | What it does | Tools |
|-------------|-------------|-------|
| **Google Calendar** | Multi-account events (read/write) | 4 |
| **Gmail** | Search, unread, semantic search (pgvector) | 8 |
| **Apple Reminders** | Read/write via EventKit (local, instant) | 4 server + 3 local |
| **Finance** | CSV import (AIB, Revolut), categorisation, analytics | 12 |
| **Obsidian Vault** | Read, write, edit, grep, list, semantic search, recent, stats | 8 |
| **WhatsApp** | Message search, contacts, semantic search | 7 |
| **Weather** | Current + 7-day forecast (Open-Meteo) | 2 |
| **Last.fm** | Listening history, stats, search | 5 |
| **Irish Rail** | Live departures from your station | 2 |
| **Apple Health** | Manual import (Health Auto Export JSON) | 7 |
| **Coffee** | Brew log + bean dial-in advisor | 13 |
| **Attachments** | Email/WhatsApp attachment ingest | 4 |
| **Historical Corpus** | Renovation document archive | 2 |
| **System** | Cross-integration diagnostics | 4 |

Plus 15 MCP prompts for orchestrated workflows (`daily_note`, `lock_in`, `process_meeting`, `weekly_review`, `finalize_week`, `quick_capture`, `add_task`, `create_note`, `search`, `week_ahead`, `listening_report`, `import_finance`, `seed_backlog`, `triage`, `plan_week`).

## Prerequisites

- **macOS** for the client (uses EventKit + launchd)
- **Python 3.12+**
- **A Linux box / VM** running Docker (Proxmox VM, Pi, etc.) for the server
- **An Obsidian vault** (or any folder of markdown files)
- **Google Cloud project** with Calendar and Gmail APIs enabled (for those integrations)

## Quick start

### 1. Deploy the server

```bash
cd server
cp .env.example .env
# Edit .env — at minimum set HOME_DB_PASSWORD and HOME_UI_TOKEN.
# HOME_MCP_TOKEN is optional (legacy/dev-only); per-user bearers in the
# client_tokens table are the canonical MCP auth path.

docker compose up -d
```

This starts the FastAPI app on port 8400 (HTTP, LAN-only) plus Postgres 16 + pgvector. See [server/README.md](server/README.md) for full server setup including Google OAuth, WhatsApp bridge, and vault mounting.

### 2. Install the client

```bash
# On each Mac
pipx install comar-client
# Or for development:
cd client && pip install -e .

comar setup
```

The setup wizard asks for your name, vault path, server URL (e.g. `http://192.168.1.50:8400` or `https://comar.lab`), and a per-device bearer token (from the `client_tokens` table on the server).

For automated/scripted installs:
```bash
comar setup --user alex --server http://192.168.1.50:8400 --token <your-token>
```

### 3. Run the machine setup script

```bash
bash scripts/setup-machine.sh
```

This installs the daemon via pipx, runs `comar setup` if `~/.config/comar/config.toml` doesn't already exist, and writes `~/.config/comar/env.sh` — then patches `~/.zshenv` to source it, so `COMAR_TOKEN` (the per-user bearer token) is exported for `.mcp.json` to pick up.

**Why `~/.zshenv` and not `~/.zshrc`**: Claude Code expands `${COMAR_TOKEN}` in `.mcp.json` from its own process environment at startup. When launched from Spotlight, the Dock, or an IDE, zsh runs `~/.zshenv` but not `~/.zshrc` — so a token exported only in `.zshrc` is invisible to GUI-launched apps. Quit and relaunch Claude Code after running the script so it picks up the token.

### 4. Start the daemon

```bash
comar install   # installs as a launchd agent (auto-starts on login)
# or
comar daemon    # foreground, for debugging
```

The daemon isn't on Claude Code's MCP path (see "How it works" above) — it needs to be running for Apple Reminders (EventKit) writes and vault-watcher re-indexing.

### 5. Connect Claude

**Claude Code** (primary, supported path): point your per-machine `.mcp.json` (gitignored, not shipped in the repo) at the server's `/mcp/` endpoint using `${COMAR_TOKEN}`:

```json
{
  "mcpServers": {
    "comar": {
      "type": "http",
      "url": "https://comar.lab/mcp/",
      "headers": {
        "Authorization": "Bearer ${COMAR_TOKEN}"
      }
    }
  }
}
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`) doesn't yet support connecting to a remote HTTP MCP server directly, so it goes through the daemon's legacy local MCP proxy instead, via `mcp-remote`:

```json
{
  "mcpServers": {
    "comar": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:9400/mcp"]
    }
  }
}
```

This Claude Desktop path is separate from the Claude Code path above and is on its own deprecation track — see `client/README.md`.

### 6. Verify

```bash
# Claude Code path — server heartbeat
curl -H "Authorization: Bearer $COMAR_TOKEN" http://192.168.1.50:8400/api/v1/heartbeat

# Claude Desktop path — daemon health
curl http://localhost:9400/health
```

## Project structure

```
comar/
├── README.md                 ← You are here
├── CLAUDE.md                 ← Operating manual (vault conventions, task system, slash commands)
├── Makefile                  ← Top-level: make build-client, make test, make deploy
├── .mcp.json                 ← Claude Code MCP config (points to localhost:9400/mcp)
├── .claude/commands/         ← Claude Code slash commands
│
├── client/                   ← comar-client Python package
│   ├── README.md
│   ├── pyproject.toml
│   └── src/comar/
│       ├── cli.py            ← CLI entry point (setup, daemon, install, status, import-health)
│       ├── daemon.py         ← Main daemon loop (MCP server + HTTP client + watcher)
│       ├── mcp_server.py     ← MCP server (Starlette + SSE/HTTP transport, /health endpoint)
│       ├── mcp_https_shim.py ← Tool proxy → server over HTTP
│       ├── server_client.py  ← HTTP client → comar-server (replaces grpc_client)
│       ├── eventkit.py       ← Apple Reminders via PyObjC EventKit
│       ├── vault_watcher.py  ← Watchdog fsevents → POST /api/v1/vault/push
│       ├── remote_logging.py ← Ships logs via /api/v1/logs/push
│       ├── updater.py        ← Auto-update (check server for new wheel, install, restart)
│       ├── launchd.py        ← launchd agent install/uninstall
│       └── default_prompts/  ← Bundled MCP prompt YAML files (15)
│
├── server/                   ← comar-server (Docker deployment)
│   ├── README.md
│   ├── CLAUDE.md             ← Deep dive on server internals
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── Makefile              ← make deploy, make deploy-fast, make deploy-pull
│   ├── .env.example
│   ├── backend/app/
│   │   ├── api/v1.py         ← V3 client API: tools, push, events, heartbeat, instructions
│   │   ├── mcp/              ← MCP server, instructions, annotations
│   │   ├── tools/            ← Declarative tool DSL (ListTool/SearchTool/SemanticSearchTool/StatsTool/CustomTool)
│   │   ├── integrations/     ← One package per integration
│   │   ├── models/           ← SQLAlchemy models
│   │   └── services/         ← Embeddings, freshness checks
│   ├── frontend/             ← React 19 dashboard (Vite, Tailwind v4)
│   └── whatsapp-bridge/      ← Node.js Baileys sidecar
│
├── .github/workflows/
│   └── deploy.yml            ← CI: build frontend + client wheel + Docker images → GHCR
│
└── vault/                    ← Obsidian vault (GITIGNORED — synced via Google Drive)
```

## Configuration

### Client (`~/.config/comar/config.toml`)

Created by `comar setup`. Example:

```toml
user = "alex"
mcp_port = 9400
auto_update = true

[server]
url = "http://192.168.1.50:8400"   # or https://comar.lab
token = "your-client-token"

[vault]
path = "/Users/you/path/to/vault"
```

### Server (`.env`)

See `server/.env.example`. Key variables:

| Variable | Required | Purpose |
|----------|----------|---------|
| `HOME_DB_PASSWORD` | Yes | Postgres password |
| `HOME_UI_TOKEN` | Yes | Web dashboard access token |
| `HOME_MCP_TOKEN` | Legacy | Pre-multi-user MCP bearer (dev/admin fallback). Per-user `client_tokens` is the live auth path. |
| `HOME_GOOGLE_CLIENT_ID` | For OAuth | Google Calendar + Gmail |
| `HOME_GOOGLE_CLIENT_SECRET` | For OAuth | Google Calendar + Gmail |
| `HOME_LASTFM_API_KEY` | For Last.fm | Last.fm scrobble history |
| `HOME_VAULTS_HOST_PATH` | For vault | Host path mounted into the container. Should contain a per-user subfolder, e.g. `<host-path>/alex/` |
| `HOME_WEATHER_LATITUDE` / `HOME_WEATHER_LONGITUDE` | No | Weather forecast location (default: Dublin) |
| `HOME_RAIL_STATION_CODE` / `HOME_RAIL_STATION_NAME` | No | Irish Rail home station (default: Malahide) |
| `HOME_TRANSFER_MATCH_NAMES` | No | Comma-separated account-holder names for internal transfer detection |

## CLI reference

```
comar setup           Interactive first-time setup
comar setup --user X --server <url> --token <token>
                      Non-interactive setup (for scripting)
comar daemon          Run daemon in foreground
comar status          Show daemon and connection status
comar install         Install launchd agent (auto-start on login)
comar uninstall       Remove launchd agent
comar import-health <file>
                      Import Apple Health Auto Export JSON
```

## The Obsidian vault

Comar is built around a shared Obsidian vault. The vault structure, task system, daily note format, and all conventions are documented in [CLAUDE.md](CLAUDE.md). Key points:

- **Two users** with private spaces (`vault/Alex/`, `vault/Sam/`) and shared areas
- **Task backlogs** — three markdown files (shared + one per person) with Obsidian Tasks plugin format
- **Daily notes** — one per person per day, created by the `daily_note` MCP prompt
- **Meeting notes** — structured from transcripts via the `process_meeting` prompt
- **Weekly reviews** — synthesised from daily notes via `weekly_review` + `finalize_week`
- **Semantic search** — vault files embedded with fastembed (BAAI/bge-small-en-v1.5) and searched via pgvector

The vault is gitignored and synced separately (Google Drive, iCloud, Syncthing).

## Development

### Run tests

```bash
make test             # All tests (client + server)
make test-client
make test-server
```

### Deploy

```bash
make deploy           # DEFAULT: pull pre-built images from GHCR + restart (after CI push)
make deploy-pull      # Alias for make deploy
make deploy-build     # Dev rsync path: build client wheel + frontend + rsync + docker build
make deploy-fast      # Dev rsync path, backend only: rsync + docker build (skip frontend)
```

### CI/CD

Pushing to `main` triggers GitHub Actions (`.github/workflows/deploy.yml`) — builds the React frontend, the client wheel (for auto-updates), and Docker images for `comar-oss-app` + `comar-oss-whatsapp`, then pushes to GHCR. Run `make deploy-pull` on the server to pull and restart.

### Auto-updates

The client checks the server for new versions on each heartbeat. If a newer wheel is available, it downloads, installs, and restarts the daemon automatically. The server serves wheels from `server/client-dist/` (not tracked in git — populated by CI or `make build-client`).

### Two-tier prompt updates

MCP prompt YAML can be updated on the server without rebuilding the client. The heartbeat carries a `prompt_set_hash` — when it changes, the client fetches the updated prompts and notifies Claude Desktop to refresh its prompt list.

## Architecture decisions

**Why HTTP+SSE?** The server runs on a home network with no public IP, but the client always initiates connections (no inbound firewall rules needed) and bearer tokens are sufficient on a LAN. HTTP keeps the surface uniform — every tool, push, and event uses the same transport — and SSE gives us a clean server→client channel without WebSockets. We previously used gRPC; the protobuf overhead and dual-transport complexity weren't paying for themselves.

**Why MCP?** Claude Desktop and Claude Code both speak MCP natively. One protocol, one endpoint, ~80 tools + 15 prompts. The client abstracts which tools are local (vault, reminders) and which are remote.

**Why Obsidian?** Markdown files are the most durable format for personal knowledge. Obsidian adds linking, search, and a plugin ecosystem. The vault is just a folder — no lock-in.

**Why EventKit instead of osascript?** PyObjC's EventKit bindings give instant read/write access to Apple Reminders — no subprocess overhead, no AppleScript parsing.

## Adapting for your family

Comar is built for a specific household but the architecture is generic:

1. **Users**: change name choices in `setup_flow.py` and the vault folder structure
2. **Integrations**: each one is self-contained in `server/backend/app/integrations/<name>/`. Disable by removing from `register_all()`, or add a new one following `BaseIntegration`
3. **Prompts**: edit YAML in `client/src/comar/default_prompts/`
4. **Location-specific**: weather coordinates (`HOME_WEATHER_LATITUDE`/`HOME_WEATHER_LONGITUDE`) and rail station (`HOME_RAIL_STATION_CODE`/`HOME_RAIL_STATION_NAME`) are set via `.env`, with sensible defaults baked in
5. **Calendar filtering**: `calendar_visibility` in `config.py` per Google account — `"full"`, `"busy"`, or `"hidden"`

## License

MIT
