# lios-sync

Local side-car daemon for the Comar (Co-Managed Archive) family knowledge system. Runs on each Mac, doing the macOS-only jobs the server can't: watching the vault for changes, writing to Apple Reminders via EventKit, and pushing Apple Health data. **It is not an MCP server** — Claude Code talks directly to the comar server's `/mcp/` endpoint (Streamable HTTP, per-user bearer). The daemon's old local MCP proxy (`mcp_server.py` / `mcp_https_shim.py`) was retired in Phase 4 (2026-07-14) after a 4-day canary logged zero non-health traffic; all that's left on the local port is a `GET /health` check.

## What it does

- **Vault watcher** — fsevents on the Obsidian vault, pushes changes to the server for re-indexing
- **Apple Reminders** dispatch via EventKit (PyObjC, instant, no AppleScript) — consumes reminders commands from the server over SSE
- **Apple Health push** — Health Auto Export → server via `/api/v1/health/push`
- **Auto-updates** — checks for a new client wheel on each heartbeat, installs and restarts
- **Remote log shipping** — logs forwarded via `POST /api/v1/logs/push` for debugging from the server side

Audio transcription has moved to a separate repo (`cograda/scribe`) and is no longer part of lios-sync.

## Install

### From PyPI / wheel

```bash
pipx install lios-sync
```

### From source (development)

```bash
cd client
pip install -e .

# With test dependencies:
pip install -e '.[test]'
```

### Via bootstrap script (from the server)

If the server is running, bootstrap a client in one command:

```bash
curl -s http://<server-ip>:8400/api/client/bootstrap.sh | bash
```

This downloads the latest wheel from the server and installs it via pipx.

## Setup

```bash
comar setup
```

Interactive wizard that configures:

| Setting | Where it's stored | What it does |
|---------|------------------|--------------|
| User name | `config.toml` → `user` | Determines vault paths (`Daily Notes/Alex/` vs `Daily Notes/Sam/`) |
| Vault path | `config.toml` → `vault.path` | Local path to the Obsidian vault |
| Server URL | `config.toml` → `server.url` | HTTP endpoint (e.g. `http://192.168.1.50:8400` or `https://comar.lab`) |
| Client token | `config.toml` → `server.token` | Per-device bearer token (created on the server) |

Config is saved to `~/.config/lios/config.toml`.

For non-interactive setup (scripting, second Mac):

```bash
comar setup --user alex --server http://192.168.1.50:8400 --token <token>
```

## Running

### As a launchd service (recommended)

```bash
comar install                  # Daemon starts automatically on login

# Logs:
tail -f ~/Library/Logs/comar/daemon.log
```

### In the foreground (debugging)

```bash
comar daemon
```

### Check status

```bash
comar status                   # CLI status check
curl localhost:9400/health     # HTTP health endpoint
```

## CLI commands

| Command | Description |
|---------|-------------|
| `lios-sync setup` | Interactive first-time configuration |
| `lios-sync daemon` | Run daemon in foreground |
| `lios-sync status` | Show connection and tool status |
| `lios-sync install` | Install launchd agent |
| `comar uninstall` | Remove launchd agent |
| `comar import-health <file>` | Import Apple Health Auto Export JSON |

## Architecture

```
comar daemon (side-car, NOT on the MCP path)
│
├── HTTP server client (server_client.py)
│   ├── Bearer token auth (per-device)
│   ├── Heartbeat loop — version check
│   ├── POST /api/v1/vault/push     — vault change notification
│   ├── POST /api/v1/health/push    — Apple Health data
│   ├── POST /api/v1/logs/push      — remote log shipping
│   └── GET  /api/v1/events (SSE)   — server→client signals (reminders commands)
│
├── Vault Watcher (watchdog / fsevents)
│   ├── Watches vault path for .md changes
│   ├── Debounces rapid changes
│   └── Pushes changed files to the server
│
├── EventKit (Apple Reminders)
│   ├── Reads all reminder lists and items
│   ├── Creates / completes / updates reminders in response to SSE events
│   └── Pushes full state on heartbeat
│
└── Updater (auto-update)
    ├── Compares local version with the server's latest
    ├── Downloads wheel from /api/client/download/latest
    ├── Installs via pip, restarts via launchctl
    └── Runs on heartbeat (~60s)
```

## Tools

Comar's MCP tools — vault search, calendar, email, finance, weather, Last.fm, WhatsApp, reminders, etc. — live on the comar server. Claude Code connects directly to the server's `/mcp/` endpoint with a per-user bearer. See `server/CLAUDE.md` in the comar repo for the full tool reference.

The daemon used to expose its own MCP server with vault_read/vault_write/vault_edit/vault_grep and reminders_add/complete/update; that surface was retired during the multi-user collapse (2026-05-02). Vault file I/O now uses Claude Code's built-in Read/Write/Edit/Grep against the local vault path; reminders writes go via the server which dispatches over SSE to this daemon.

**Phase 4 (2026-07-14):** the daemon's remaining local MCP proxy (which merged those local tools with proxied server tools on `localhost:9400/mcp`) and its bundled MCP prompt mirroring (`~/.config/lios/prompts/`) were deleted outright — a `CanaryLoggingMiddleware` instrumented the proxy for 4 days and logged zero non-health requests, confirming nothing (not Claude Code, not Claude Desktop) was still calling it. The daemon is now a pure side-car: EventKit execution via server SSE commands, the vault fsevents watcher, heartbeat/auto-update, and remote log shipping. `GET /health` is the only thing left on the old `mcp_port`.

## Config file reference

`~/.config/lios/config.toml`:

```toml
user = "alex"              # Used for vault paths and server identification
mcp_port = 9400            # Local health-check port (GET /health only, default 9400). health_port also accepted.
auto_update = true         # Auto-install new versions from the server

[server]
url = "http://192.168.1.50:8400"   # or https://comar.lab
token = "your-token"              # Per-device bearer token

[vault]
path = "/path/to/vault"
```

## Logs

Daemon logs land at `~/Library/Logs/comar/daemon.log`. They're also shipped to the server via `POST /api/v1/logs/push` for remote debugging.

## Troubleshooting

**Daemon won't start**: check `~/Library/Logs/comar/daemon.log`. Common issues:
- Missing `config.toml` → run `lios-sync setup`
- Port 9400 in use → check for a stale process (`lsof -i :9400`)
- Can't reach server → verify URL/token, try `lios-sync status`

**Reminders not working**: macOS requires explicit permission. System Settings → Privacy & Security → Reminders, grant access to Python/Terminal.

**Auto-update not working**: the daemon checks the server's `/api/client/version` endpoint. Ensure the server has a wheel in `server/client-dist/` and that `auto_update = true` in config.

## Development

```bash
pip install -e '.[test]'
python -m pytest tests/ -v

# Build a wheel
pip install hatchling
python -m hatchling build -t wheel
```

Version is read from `src/comar/__init__.py` (`__version__`).
