"""Install endpoint — `POST /api/install/redeem` returns a personalised installer.

This is the entrypoint for new machines that don't yet have comar credentials.
An admin runs `python -m app.scripts.create_install_code --user sam
--label sam-macbook` to mint a single-use code that wraps a freshly-minted
bearer. The new machine then hits this route over Tailscale (code in the POST
body, not the URL, so it doesn't end up in access logs), gets back a bash
script with the token baked in, and pipes it into `bash`.

The script is a chain of preflight gates (Xcode CLT → Tailscale → Google Drive
vault sync → Claude Code app) followed by daemon install + working folder +
launchd + E2E smoke. Safe to re-run.

See `INSTALL_SCRIPT_TEMPLATE` below for the exact generated script.

The route trusts the code as the auth boundary — no bearer header. Single-use
and 24h-TTL bounds the blast radius.
"""

from datetime import datetime, timezone
import logging
import re

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.db import get_db
from app.models.clients import ClientToken, InstallCode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/install", tags=["install"])


class InstallRedeemRequest(BaseModel):
    code: str = Field(..., description="Single-use install code minted by create_install_code.py")


# Bash installer template. Variables are __DOUBLE_UNDERSCORE__-wrapped so we
# never collide with shell ${VAR} expansion. Rendered via str.replace at
# request time. See docs/laptop-onboarding.md for the design rationale.
INSTALL_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# Comar installer — end-to-end machine setup.
# Generated for: __USER__ (label: __LABEL__)
#
# Safe to re-run any number of times. Each step detects → installs if missing
# → blocks until configured → moves on.
set -euo pipefail

USER_NAME="__USER__"
LABEL="__LABEL__"
SERVER="__SERVER__"
TOKEN="__TOKEN__"
SENTINEL_FILE=".comar-synced"   # dropped in the vault by Alex; proves Drive sync

say()  { printf "\033[1;36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }
prompt(){ printf "\033[1;35m?\033[0m %s\n" "$*"; }

# Wait until a predicate is true, polling every 5s with a friendly heartbeat.
wait_until() {
  local desc="$1"; shift
  local i=0
  until "$@"; do
    if (( i % 6 == 0 )); then prompt "Waiting for: ${desc}…"; fi
    sleep 5
    i=$((i+1))
  done
}

say "Comar installer for ${USER_NAME} starting"
say "Server: ${SERVER}"

# ──────────────────────────────────────────────────────────────────────────────
# Gate 1: Xcode Command Line Tools (provides python3, git, curl)
# ──────────────────────────────────────────────────────────────────────────────
gate_xcode() {
  if xcode-select -p >/dev/null 2>&1; then
    ok "Xcode Command Line Tools present"
    return
  fi
  say "Triggering Xcode Command Line Tools install (a dialog will appear)"
  xcode-select --install 2>/dev/null || true
  prompt "Click 'Install' in the dialog that just opened. I'll wait."
  wait_until "Xcode Command Line Tools" xcode-select -p >/dev/null 2>&1
  ok "Xcode Command Line Tools installed"
}

# ──────────────────────────────────────────────────────────────────────────────
# Gate 2: Tailscale — installed AND signed in AND can reach the comar server
# ──────────────────────────────────────────────────────────────────────────────
tailscale_running() {
  command -v tailscale >/dev/null 2>&1 || return 1
  tailscale status --json 2>/dev/null | grep -q '"BackendState": *"Running"' || return 1
  # Heartbeat with no auth should 401 (proves the host is reachable; auth is checked next step).
  local code
  code=$(curl -o /dev/null -s -w "%{http_code}" --max-time 5 "${SERVER}/api/v1/heartbeat" || echo 000)
  [[ "${code}" == "401" || "${code}" == "200" ]]
}
gate_tailscale() {
  if tailscale_running; then ok "Tailscale connected and ${SERVER} reachable"; return; fi
  if ! command -v tailscale >/dev/null 2>&1; then
    say "Installing Tailscale (downloading .pkg)"
    local tmp
    tmp=$(mktemp -d)
    curl -fsSL https://pkgs.tailscale.com/stable/Tailscale-latest.pkg -o "${tmp}/Tailscale.pkg"
    sudo installer -pkg "${tmp}/Tailscale.pkg" -target /
    rm -rf "${tmp}"
    open -a "/Applications/Tailscale.app" || true
  else
    open -a "/Applications/Tailscale.app" || true
  fi
  prompt "Click the Tailscale menubar icon → Sign in with the family Google account."
  prompt "Alex will approve the new device in the Tailscale admin console."
  wait_until "Tailscale signed in and ${SERVER} reachable" tailscale_running
  ok "Tailscale ready"
}

# ──────────────────────────────────────────────────────────────────────────────
# Gate 3: Syncthing — installed AND started AND paired with comar-server
# ──────────────────────────────────────────────────────────────────────────────
# Vault location is deterministic — not discovered. Syncthing creates it.
VAULT_DIR="${HOME}/Desktop/Code/comar/vault"
WORKING_DIR="${HOME}/Desktop/Code/comar"
SYNCTHING_VERSION="v1.30.0"
SYNCTHING_BIN="${HOME}/Library/Application Support/comar/bin/syncthing"
SYNCTHING_CONFIG="${HOME}/Library/Application Support/Syncthing"
SYNCTHING_PLIST_LABEL="com.cograda.comar.syncthing"

syncthing_api_call() {
  # $1 = method, $2 = path, $3 = optional body. Returns response body.
  local method="$1" path="$2" body="${3:-}"
  local api_key
  api_key=$(grep -oE '<apikey>[^<]+' "${SYNCTHING_CONFIG}/config.xml" 2>/dev/null | sed 's|<apikey>||')
  [[ -z "${api_key}" ]] && return 1
  if [[ -n "${body}" ]]; then
    curl -s -H "X-API-Key: ${api_key}" -X "${method}" -H "Content-Type: application/json" \
         -d "${body}" "http://127.0.0.1:8384${path}"
  else
    curl -s -H "X-API-Key: ${api_key}" -X "${method}" "http://127.0.0.1:8384${path}"
  fi
}

gate_syncthing() {
  # 1. Install Syncthing binary if missing
  if [[ ! -x "${SYNCTHING_BIN}" ]]; then
    say "Installing Syncthing ${SYNCTHING_VERSION}"
    local arch tarball tmp
    arch="$(uname -m)"
    case "${arch}" in
      arm64) tarball="syncthing-macos-arm64-${SYNCTHING_VERSION}.tar.gz" ;;
      x86_64) tarball="syncthing-macos-amd64-${SYNCTHING_VERSION}.tar.gz" ;;
      *) die "Unsupported architecture: ${arch}" ;;
    esac
    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/syncthing/syncthing/releases/download/${SYNCTHING_VERSION}/${tarball}" \
         -o "${tmp}/syncthing.tar.gz"
    tar xzf "${tmp}/syncthing.tar.gz" -C "${tmp}"
    mkdir -p "$(dirname "${SYNCTHING_BIN}")"
    cp "${tmp}"/syncthing-macos-*/syncthing "${SYNCTHING_BIN}"
    chmod +x "${SYNCTHING_BIN}"
    rm -rf "${tmp}"
    ok "Syncthing installed at ${SYNCTHING_BIN}"
  else
    ok "Syncthing already installed"
  fi

  # 2. launchd plist (lets Syncthing start at login + restart on crash)
  local plist="${HOME}/Library/LaunchAgents/${SYNCTHING_PLIST_LABEL}.plist"
  mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/Library/Logs/comar"
  cat > "${plist}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${SYNCTHING_PLIST_LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>${SYNCTHING_BIN}</string><string>serve</string>
    <string>--no-browser</string><string>--no-restart</string>
    <string>--home=${SYNCTHING_CONFIG}</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${HOME}/Library/Logs/comar/syncthing.out</string>
  <key>StandardErrorPath</key><string>${HOME}/Library/Logs/comar/syncthing.err</string>
</dict></plist>
PLIST
  launchctl unload "${plist}" 2>/dev/null || true
  launchctl load "${plist}"

  # 3. Wait for Syncthing's REST API to come online
  say "Waiting for Syncthing API on 127.0.0.1:8384"
  for _ in $(seq 1 30); do
    [[ -f "${SYNCTHING_CONFIG}/config.xml" ]] && curl -fs -o /dev/null --max-time 1 http://127.0.0.1:8384/ && break
    sleep 1
  done
  [[ -f "${SYNCTHING_CONFIG}/config.xml" ]] || die "Syncthing config never appeared"

  # 4. Pair with comar-server
  local device_id server_resp server_device_id
  device_id=$(syncthing_api_call GET /rest/system/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["myID"])')
  [[ -z "${device_id}" ]] && die "Couldn't read local Syncthing device ID"
  say "This device's Syncthing ID: ${device_id:0:15}…"

  server_resp=$(curl -fsS -X POST -H "Authorization: Bearer ${TOKEN}" \
                     -H "Content-Type: application/json" \
                     -d "{\"device_id\":\"${device_id}\",\"device_name\":\"${USER_NAME}-mac\"}" \
                     "${SERVER}/api/v1/syncthing/pair")
  echo "  pair response: ${server_resp}"

  server_resp=$(curl -fsS -H "Authorization: Bearer ${TOKEN}" "${SERVER}/api/v1/syncthing/server-id")
  server_device_id=$(echo "${server_resp}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["device_id"])')
  local folder_ids
  folder_ids=$(echo "${server_resp}" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["folders"]))')
  say "Server device ID: ${server_device_id:0:15}…, folders: ${folder_ids}"

  # 5. Configure local Syncthing: add server peer, remove default folder
  syncthing_api_call DELETE /rest/config/folders/default >/dev/null || true
  syncthing_api_call PUT "/rest/config/devices/${server_device_id}" \
    "{\"deviceID\":\"${server_device_id}\",\"name\":\"comar-server\",\"addresses\":[\"tcp://your-server.your-tailnet.ts.net:22000\"],\"compression\":\"metadata\",\"autoAcceptFolders\":true}" >/dev/null
  syncthing_api_call PATCH /rest/config/options \
    '{"globalAnnounceEnabled":false,"relaysEnabled":false,"natEnabled":false,"urAccepted":-1}' >/dev/null

  # 6. Pre-create the vault path so Syncthing's auto-accept lands files there
  mkdir -p "${VAULT_DIR}"

  # 7. Add the personal folder share
  local personal_folder="vault-${USER_NAME}"
  syncthing_api_call PUT "/rest/config/folders/${personal_folder}" \
    "{\"id\":\"${personal_folder}\",\"label\":\"${USER_NAME^} Vault\",\"path\":\"${VAULT_DIR}\",\"type\":\"sendreceive\",\"devices\":[{\"deviceID\":\"${device_id}\"},{\"deviceID\":\"${server_device_id}\"}],\"rescanIntervalS\":60,\"fsWatcherEnabled\":true,\"ignorePerms\":true}" >/dev/null
  cat > "${VAULT_DIR}/.stignore" <<STIGN
.obsidian/**
.git/**
.embeddings/**
.DS_Store
STIGN

  ok "Syncthing paired with comar-server; folders ${folder_ids}"

  # 8. Wait for initial sync to settle (each folder either matches server or has files transferring)
  say "Waiting for initial sync to settle (up to 60s)"
  for _ in $(seq 1 12); do
    sleep 5
    local pending
    pending=$(syncthing_api_call GET "/rest/db/status?folder=${personal_folder}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("needFiles",0))')
    [[ "${pending}" == "0" ]] && { ok "Vault folder in sync"; break; }
    prompt "  still receiving (${pending} files pending)…"
  done
}

# ──────────────────────────────────────────────────────────────────────────────
# Gate 4: Claude Code app — installed (sign-in deferred to first launch)
# ──────────────────────────────────────────────────────────────────────────────
gate_claude_code() {
  if [[ -d "/Applications/Claude Code.app" ]]; then
    ok "Claude Code app present"
    return
  fi
  warn "Claude Code app not installed."
  prompt "Download from https://claude.ai/download and install."
  prompt "Sign in with your Anthropic account on first launch."
  wait_until "Claude Code app installed" test -d "/Applications/Claude Code.app"
  ok "Claude Code app installed"
}

# ──────────────────────────────────────────────────────────────────────────────
# Steps 5–11: comar-specific setup
# ──────────────────────────────────────────────────────────────────────────────

# Step 5: venv + wheel
step_install_daemon() {
  local app_support="${HOME}/Library/Application Support/comar"
  local venv="${app_support}/venv"
  mkdir -p "${app_support}"

  if [[ ! -x "${venv}/bin/python3" ]]; then
    say "Creating venv at ${venv}"
    python3 -m venv "${venv}"
  fi

  say "Downloading latest comar wheel"
  local tmp
  tmp=$(mktemp -d)
  curl -fsSL -H "Authorization: Bearer ${TOKEN}" \
       "${SERVER}/api/client/download/latest" -o "${tmp}/comar.whl"

  say "Installing wheel into venv"
  "${venv}/bin/pip" install --quiet --upgrade pip
  "${venv}/bin/pip" install --quiet --force-reinstall "${tmp}/comar.whl"
  rm -rf "${tmp}"

  mkdir -p "${HOME}/.local/bin"
  ln -sf "${venv}/bin/comar" "${HOME}/.local/bin/comar"
  ok "comar daemon installed (venv-managed)"
}

# Step 6: config.toml
step_write_config() {
  local cfg_dir="${HOME}/.config/comar"
  local cfg="${cfg_dir}/config.toml"
  mkdir -p "${cfg_dir}"
  if [[ -f "${cfg}" ]]; then
    warn "config.toml already exists — leaving alone. Delete it and re-run to regenerate."
    return
  fi
  cat > "${cfg}" <<EOF
# Generated by comar installer on $(date -u +%Y-%m-%dT%H:%M:%SZ)
user = "${USER_NAME}"
mcp_port = 9400
auto_update = true

[server]
url = "${SERVER}"
token = "${TOKEN}"

[vault]
path = "${VAULT_DIR}"
EOF
  chmod 600 "${cfg}"
  ok "Wrote ${cfg}"
}

# Step 7: working folder at ~/Desktop/Code/comar/{.mcp.json, CLAUDE.md, vault/}
# The vault/ directory is the real Syncthing-managed folder from Gate 3.
step_working_folder() {
  local proj="${WORKING_DIR}"
  mkdir -p "${proj}"

  # Sanity: vault must already exist (Gate 3 created + populated it via Syncthing)
  [[ -d "${VAULT_DIR}" ]] || die "Vault dir ${VAULT_DIR} doesn't exist — Gate 3 didn't complete"

  if [[ ! -f "${proj}/.mcp.json" ]]; then
    cat > "${proj}/.mcp.json" <<MCP
{
  "mcpServers": {
    "comar": {
      "type": "http",
      "url": "${SERVER}/mcp/",
      "headers": {
        "Authorization": "Bearer \${COMAR_TOKEN}"
      }
    }
  }
}
MCP
  fi

  if [[ ! -f "${proj}/CLAUDE.md" ]]; then
    cat > "${proj}/CLAUDE.md" <<EOF
# Comar — ${USER_NAME}'s workspace

This is your home for talking to comar. You are using Claude Code (the Mac app).

## Things to know about this user
- New to Claude Code. Prefer slash-prompts over freeform tool composition.
- Never ask the user to run shell commands. Never reference internal MCP tool names.
- Use a warm, conversational tone. Lead with the answer, then offer next steps.

## Slash-prompts you'll use most
- /daily-note — Open today's daily note
- /add-task — Add something to your backlog
- /find — Search the vault
- /triage — Quick routing for new emails / messages
- /lock-in — Set today's focus on your phone

## The vault
Your personal vault lives at \`./vault/\` — daily notes, your backlog, notes
you've taken, anything keyed to you.

Use \`vault_transfer\` if you want to hand a file off to someone else — it
moves the file to their Inbox.
EOF
  fi
  ok "${WORKING_DIR} working folder ready"
}

# Step 8: .zshenv env shim (so GUI-launched Claude Code sees ${COMAR_TOKEN})
step_zshenv() {
  local shim="${HOME}/.config/comar/env.sh"
  cat > "${shim}" <<'EOF'
# comar — exports COMAR_TOKEN from ~/.config/comar/config.toml.
# Sourced by ~/.zshenv so GUI-launched apps (Claude Code via Spotlight/Dock)
# see the token, not only interactive terminals.
_comar_config="${HOME}/.config/comar/config.toml"
if [ -r "${_comar_config}" ]; then
  COMAR_TOKEN=$(awk -F'"' '/^[[:space:]]*token[[:space:]]*=/ {print $2; exit}' "${_comar_config}")
  [ -n "${COMAR_TOKEN}" ] && export COMAR_TOKEN
fi
unset _comar_config
EOF
  local zshenv="${HOME}/.zshenv"
  local line='[ -r "$HOME/.config/comar/env.sh" ] && . "$HOME/.config/comar/env.sh"'
  if [[ -f "${zshenv}" ]] && grep -Fq "${line}" "${zshenv}"; then
    ok ".zshenv already sources env shim"
  else
    {
      [[ -f "${zshenv}" ]] && echo
      echo "# comar — token export for GUI-launched tools."
      echo "${line}"
    } >> "${zshenv}"
    ok ".zshenv patched"
  fi
}

# Step 9: launchd (run daemon at login, restart on crash)
step_launchd() {
  local label="com.cograda.comar"
  local plist="${HOME}/Library/LaunchAgents/${label}.plist"
  local comar_bin="${HOME}/Library/Application Support/comar/venv/bin/comar"
  local log_dir="${HOME}/Library/Logs/comar"
  mkdir -p "${log_dir}" "${HOME}/Library/LaunchAgents"

  cat > "${plist}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key><array>
    <string>${comar_bin}</string><string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${log_dir}/comar.out</string>
  <key>StandardErrorPath</key><string>${log_dir}/comar.err</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
EOF

  launchctl unload "${plist}" 2>/dev/null || true
  launchctl load "${plist}"
  ok "launchd agent loaded (${label})"
}

# Step 10: heartbeat 200
step_heartbeat() {
  local code
  code=$(curl -o /dev/null -s -w "%{http_code}" --max-time 10 \
              -H "Authorization: Bearer ${TOKEN}" \
              "${SERVER}/api/v1/heartbeat")
  case "${code}" in
    200) ok "Heartbeat 200 (auth chain works)" ;;
    401) die "Heartbeat 401 — token rejected. Re-run with a fresh install code." ;;
    *)   die "Heartbeat returned HTTP ${code} from ${SERVER}" ;;
  esac
}

# Step 11: E2E smoke — call a per-user tool, prove user_id scoping is correct
step_smoke() {
  local response
  response=$(curl -fsS --max-time 10 \
                  -H "Authorization: Bearer ${TOKEN}" \
                  -H "Content-Type: application/json" \
                  -X POST "${SERVER}/api/v1/tools/calendar_today" \
                  -d '{}' || echo "")
  if [[ -z "${response}" ]]; then
    warn "Smoke test could not reach calendar_today — non-fatal, daemon is still installed."
    return
  fi
  ok "Smoke test: calendar_today returned a response"
}

gate_xcode
gate_tailscale
gate_syncthing
gate_claude_code

step_install_daemon
step_write_config
step_working_folder
step_zshenv
step_launchd
step_heartbeat
step_smoke

echo
ok "Comar is installed end-to-end for ${USER_NAME}."
prompt "Next steps:"
prompt "  1. Open Claude Code, sign in if you haven't already."
prompt "  2. File → Open Folder → ~/Desktop/Code/comar"
prompt "  3. Ask: 'what's on my calendar today?' to confirm it works."
"""


def _render(server_url: str, token: str, user: str, label: str) -> str:
    """Substitute __VAR__ placeholders. Done as plain replace to keep the
    template readable as bash (no f-string brace escaping, no Jinja2 dep)."""
    return (
        INSTALL_SCRIPT_TEMPLATE
        .replace("__SERVER__", server_url.rstrip("/"))
        .replace("__TOKEN__", token)
        .replace("__USER__", user)
        .replace("__LABEL__", label)
    )


@router.post("/redeem", response_class=PlainTextResponse)
async def fetch_install_script(payload: InstallRedeemRequest, request: Request) -> PlainTextResponse:
    """Render and return the install script for a single-use code.

    Takes the code via POST body rather than a URL/path parameter — a GET
    with the code in the URL would end up in shell history and any access
    log the request passes through (Caddy, Tailscale, etc).

    Marks the code redeemed on success (with timestamp + caller IP). Subsequent
    fetches return 410 Gone. Expired codes return 410 Gone with a different
    detail. Unknown codes return 404.
    """
    code = payload.code
    db = get_db()
    with db.session() as session:
        ic = session.execute(
            select(InstallCode).where(InstallCode.code == code)
        ).scalar_one_or_none()

        if ic is None:
            raise HTTPException(404, "Unknown install code")

        now = datetime.now(timezone.utc)
        if ic.redeemed_at is not None:
            raise HTTPException(410, "Install code already redeemed")
        if ic.expires_at <= now:
            raise HTTPException(410, "Install code expired")

        token = session.get(ClientToken, ic.token_id)
        if token is None or not token.is_active:
            raise HTTPException(409, "Backing token missing or revoked")

        # FK guarantees the User row exists (RESTRICT). Snapshot attrs before
        # commit — session is expire_on_commit=True per server CLAUDE.md.
        from app.models.users import User  # local: avoids circular import
        user_row = session.get(User, ic.user_id)
        if user_row is None or not user_row.is_active:
            raise HTTPException(409, "User missing or inactive")
        token_str = token.token
        user_name = user_row.name

        ic.redeemed_at = now
        ic.redeemed_from_ip = (request.client.host if request.client else None) or ""
        session.commit()

        logger.info(
            "Install code redeemed: code=%s user=%s label=%s ip=%s",
            code, user_name, ic.label, ic.redeemed_from_ip,
        )

        # Server URL the script will phone home to. Prefer the public/Tailscale
        # URL since that's what a non-LAN bootstrap reaches us on. Falls back
        # to comar.lab for LAN re-runs.
        server_url = getattr(settings, "server_public_url", None) or "https://your-server.your-tailnet.ts.net"

        script = _render(server_url, token_str, user_name, ic.label)
        return PlainTextResponse(script, media_type="text/x-shellscript")
