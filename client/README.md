# comar-client

Local side-car daemon for the Comar (Co-Managed Archive) family knowledge system. Runs on each Mac, doing the macOS-only jobs the server can't: watching the vault for changes, writing to Apple Reminders via EventKit, and pushing Apple Health data. **It is no longer an MCP server** — Claude Code talks directly to the comar server's `/mcp/` endpoint (Streamable HTTP, per-user bearer). The local MCP shim in `mcp_server.py` is legacy code awaiting deletion (see ISS-002).

## What it does

- **Vault watcher** — fsevents on the Obsidian vault, pushes changes to the server for re-indexing
- **Apple Reminders** dispatch via EventKit (PyObjC, instant, no AppleScript) — consumes reminders commands from the server over SSE
- **Apple Health push** — Health Auto Export → server via `/api/v1/health/push`
- **Auto-updates** — checks for a new client wheel on each heartbeat, installs and restarts
- **Remote log shipping** — logs forwarded via `POST /api/v1/logs/push` for debugging from the server side

Audio transcription has moved to a separate repo (`cograda/scribe`) and is no longer part of comar-client.

## Install

### From PyPI / wheel

```bash
pipx install comar-client
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

Config is saved to `~/.config/comar/config.toml`.

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
| `comar setup` | Interactive first-time configuration |
| `comar daemon` | Run daemon in foreground |
| `comar status` | Show connection and tool status |
| `comar install` | Install launchd agent |
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

The daemon used to expose its own MCP server with vault_read/vault_write/vault_edit/vault_grep and reminders_add/complete/update; that surface was retired during the multi-user collapse (2026-05-02). Vault file I/O now uses Claude Code's built-in Read/Write/Edit/Grep against the local vault path; reminders writes go via the server which dispatches over SSE to this daemon. The legacy `mcp_server.py` + `mcp_https_shim.py` code is unreferenced and tracked for deletion as ISS-002.

## MCP prompts

15 prompt workflows are bundled and automatically deployed to `~/.config/comar/prompts/`. They appear in Claude Desktop's prompt picker (the `/` menu). See the root [README](../README.md) for the full list.

Prompts are YAML with Jinja-style template variables:

```yaml
name: daily_note
title: "Morning Daily Note"
description: "Create today's daily note with calendar, health, email, and task carry-forward"
arguments:
  - name: user
    description: "Whose note: alex or sam"
    required: false
messages:
  - role: user
    content: |
      Create today's daily note for {{ user }}.
      ...orchestration instructions...
```

## Config file reference

`~/.config/comar/config.toml`:

```toml
user = "alex"              # Used for vault paths and server identification
mcp_port = 9400            # MCP server port (default 9400)
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
- Missing `config.toml` → run `comar setup`
- Port 9400 in use → check for a stale process (`lsof -i :9400`)
- Can't reach server → verify URL/token, try `comar status`

**No proxied tools**: the daemon starts with local tools only and fetches server tools on first heartbeat. If `tools_proxied` is 0, check server connectivity.

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
