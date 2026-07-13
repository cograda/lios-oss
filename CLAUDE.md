# Comar — Operating Manual

**Co-Managed Archive** — from the Irish *comar* (confluence, meeting place).

Unified family knowledge system. Client-server architecture with three components: the Obsidian vault (knowledge layer), a remote server (data engine + canonical MCP), and a local client daemon that runs as a side-car for things that physically need to happen on the Mac (EventKit + vault file watcher).

## Architecture

```
Claude Code ── MCP / Streamable HTTP ── comar-server (8400/mcp/, per-user bearer)
                                              │
                                              │ tools (~80, annotated read/write/destructive)
                                              │   • vault_search / vault_recent / vault_stats
                                              │   • reminders_add / complete / update
                                              │     └─ server enqueues → SSE callback to daemon → EventKit
                                              │   • calendar / mail / finance / health / weather / …
                                              ▼
                                  Postgres 16 + pgvector

comar-client (daemon, localhost:9400) — side-car; still boots a legacy localhost
                                        MCP proxy pending Phase-4 retirement (canary running)
  ├── Vault watcher: fsevents → POST /api/v1/vault/push (re-embed)
  ├── Reminders: subscribes to server SSE callbacks → executes EventKit (PyObjC)
  └── /api/v1/heartbeat for client liveness

Vault file edits in Claude Code use the native filesystem tools (Read / Edit /
Write / Grep) against the vault path on disk. The daemon's watcher picks up
the change and pushes it to the server for re-indexing.
```

**Key properties:**
- One MCP endpoint, one transport: **Streamable HTTP** at `/mcp/`, per-user bearer auth (post `bd65c42`). Claude Code's `.mcp.json` points directly at the server. The daemon still runs a legacy localhost MCP proxy app; a canary (started 2026-07-10, `mcp-canary:` log lines) verifies nothing uses it before Phase 4 deletes it.
- The daemon exists for two macOS-only jobs: **EventKit** (Apple Reminders write-back) and **fsevents** (vault file change push). Everything else moved to the server.
- ~80 tools, every one carries MCP `annotations` (readOnlyHint / destructiveHint / idempotentHint / openWorldHint).
- Single instructions block (`server/backend/app/mcp/instructions.py`) is what the model sees on connect.
- Audio transcription lives in a separate repo (`cograda/scribe`) — comar-client is single-purpose.

## Monorepo Layout

```
Code/comar/                              ← Git repo (cograda/comar)
├── CLAUDE.md                            ← This file
├── .claude/commands/                    ← Slash commands
├── .mcp.json                            ← Per-machine, gitignored. Streamable HTTP → server /mcp/, ${COMAR_TOKEN} bearer
├── Makefile                             ← make build-client, make deploy, make test
├── client/                              ← comar-client Python package
│   ├── pyproject.toml
│   └── src/comar/                       ← CLI, daemon, MCP server, HTTP server client
│       ├── server_client.py             ← HTTP client → comar-server (replaces grpc_client)
│       ├── mcp_https_shim.py            ← MCP tool proxy over HTTP
│       ├── eventkit.py                  ← Apple Reminders via PyObjC (local)
│       ├── vault_watcher.py             ← fsevents → /api/v1/vault/push
│       └── ...
├── server/                              ← comar-server (FastAPI)
│   ├── backend/app/
│   │   ├── api/v1.py                    ← V3 client API (tools, push, events SSE, etc.)
│   │   ├── mcp/                         ← Tool registry, instructions, annotations
│   │   ├── tools/                       ← Declarative DSL (ListTool/SearchTool/etc.)
│   │   └── integrations/                ← One package per integration
│   ├── frontend/                        ← React dashboard
│   ├── Dockerfile, docker-compose.yml
│   └── csv-inbox/                       ← Finance CSV import inbox
└── vault/                               ← Obsidian vault (GITIGNORED, synced via Google Drive)
```

- **vault/** is gitignored. Syncs to server via Google Drive + rclone.
- **client/** is the local daemon — install with `pipx install comar-client`.
- **server/** deploys to `SERVER_IP` via Docker. See `server/CLAUDE.md` for server-specific docs.
- **scribe** (audio transcription) lives in a separate repo at `Code/scribe/` — drops markdown into a watched folder.

## New Machine Setup

Per-machine config lives outside the repo (token, vault path, server URL). The repo carries the **spec** (`client/config.example.toml`) and the **bootstrap script** (`scripts/setup-machine.sh`).

```bash
git clone git@github.com:cograda/comar.git
cd comar
bash scripts/setup-machine.sh
# → prompts for user (alex/sam), server URL, bearer token
# → installs the daemon via pipx
# → writes ~/.config/comar/config.toml
# → writes ~/.config/comar/env.sh and patches ~/.zshenv to source it
# → verifies with a server heartbeat
# Quit and relaunch Claude Code afterwards so it picks up COMAR_TOKEN.
```

**Why `~/.zshenv` and not `~/.zshrc`:** Claude Code expands `${COMAR_TOKEN}` in `.mcp.json` from its own process environment at startup. When launched from Spotlight/Dock/an IDE, zsh runs `~/.zshenv` but **not** `~/.zshrc` — so token exports in `.zshrc` are invisible to GUI-launched apps. Putting the source line in `~/.zshenv` is the fix.

**Single source of truth:** the per-user bearer token lives only in `~/.config/comar/config.toml` (used by the daemon). `~/.config/comar/env.sh` reads it from there and exports `COMAR_TOKEN` for `.mcp.json` consumers. Don't paste tokens into `.zshrc`/`.zshenv` directly.

**Per-machine vs portable:**

| Bit | Where | In git? |
|---|---|---|
| `.mcp.json` (uses `${COMAR_TOKEN}`) | repo | yes |
| Daemon config schema | `client/config.example.toml` | yes |
| Bootstrap orchestrator | `scripts/setup-machine.sh` | yes |
| Real config (with token + vault path) | `~/.config/comar/config.toml` | no |
| Env shim | `~/.config/comar/env.sh` | no |
| `.zshenv` source line | `~/.zshenv` | no |

If MCP isn't connecting in a Claude Code session, check (in order): `echo $COMAR_TOKEN` is non-empty in the *exact* shell that launched Claude Code; `curl -H "Authorization: Bearer $COMAR_TOKEN" $SERVER_URL/api/v1/heartbeat` returns 200; you're on home Wi-Fi or Tailscale is up.

---

## Where to look

This file (root `CLAUDE.md`) covers only the cross-cutting facts: architecture, monorepo layout, machine setup. Specialised detail lives in two places:

| For | See |
|---|---|
| **Vault rules** — folder structure, daily notes, task system, meeting processing, frontmatter schemas, tagging, naming, what-not-to-do | `vault/CLAUDE.md` |
| **Server detail** — API endpoints, MCP tool registry, integrations, scheduling, deploy commands, known issues, sub-package conventions | `server/CLAUDE.md` (and `server/coglib/CLAUDE.md` for the shared lib) |
| **Project history** — what's shipped and when, including V3 transport rewrite (2026-04-28), multi-user A→D + collapse-to-one-MCP Steps 1–3 (2026-05-02), and ongoing entries | `vault/Projects/Comar/Changelog.md` |
| **In-flight plans** | `vault/Projects/Comar/Plans/` |
| **Shipped plans** (preserved as historical reference) | `vault/Projects/Comar/Plans/Shipped/` |
| **Hub note** | `vault/Projects/Comar/Comar.md` |

Convention: when a plan ships, update its frontmatter to `status: shipped` + `shipped: YYYY-MM-DD`, move it to `Plans/Shipped/`, and add a one-paragraph entry to the Changelog. The Changelog *is* the project change list.

---
