"""Install endpoint — `GET /api/install/{code}` returns a personalised installer.

This is the entrypoint for new machines that don't yet have lios credentials.
An admin runs `python -m app.scripts.create_install_code --user sam
--label sam-macbook` to mint a single-use code that wraps a freshly-minted
bearer. The new machine then hits this route over Tailscale, gets back a bash
script with the token baked in, and pipes it into `bash`.

The script is a chain of preflight gates (Xcode CLT → Tailscale → Syncthing
vault sync → Claude Code app) followed by daemon install + working folder +
launchd + E2E smoke. Safe to re-run.

The route trusts the code as the auth boundary — no bearer header. Single-use
and 24h-TTL bounds the blast radius.
"""

from datetime import datetime, timezone
import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

from app.auth.encryption import decrypt_token
from app.auth.rate_limit import is_over_limit, record_failure
from app.config import settings
from app.db import get_db
from app.models.clients import ClientToken, InstallCode
from app.auth.client_token import get_current_user
from app.auth.hashing import hash_token
from app.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/install", tags=["install"])


# Bash installer template. Variables are __DOUBLE_UNDERSCORE__-wrapped so we
# never collide with shell ${VAR} expansion. Rendered via str.replace at
# request time. See docs/sam-laptop-onboarding.md for the design rationale.
INSTALL_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# lios installer — end-to-end machine setup (was the Comar installer until 2026-09-02).
# Generated for: __USER__ (label: __LABEL__)
#
# Safe to re-run any number of times. Each step detects → installs if missing
# → blocks until configured → moves on.
set -euo pipefail

USER_NAME="__USER__"
# macOS ships bash 3.2 and this script runs under it on a fresh Mac. No bash-4
# expansions (${var^}, ${var,,}, declare -A, mapfile, |&): ${USER_NAME^} was a
# "bad substitution" that killed Sam's install mid-pairing on 2026-09-06 —
# invisible on Alex's Mac, where Homebrew's bash 5 is first on PATH.
USER_LABEL="$(printf '%s' "${USER_NAME:0:1}" | tr '[:lower:]' '[:upper:]')${USER_NAME:1}"
LABEL="__LABEL__"
SERVER="__SERVER__"
TOKEN="__TOKEN__"

# Working folder + vault location. Deterministic, not discovered — Syncthing
# (Gate 3) creates VAULT_DIR; step_working_folder creates WORKING_DIR around it.
# ~/lios for new machines. A machine set up before 2026-09-02 has ~/Comar (its
# vault is a Syncthing folder there, so it is not renamed by a script); the
# installer keeps using it until Alex moves it by hand.
if [ -d "${HOME}/Comar" ] && [ ! -d "${HOME}/lios" ]; then
  WORKING_DIR="${HOME}/Comar"
else
  WORKING_DIR="${HOME}/lios"
fi
VAULT_DIR="${WORKING_DIR}/vault"

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

say "lios installer for ${USER_NAME} starting"
say "Server: ${SERVER}"

# ──────────────────────────────────────────────────────────────────────────────
# Gate 0: clean-slate — retire pre-2026-09-02 "comar" residue before anything
# else runs. A machine visited before the platform rename (e.g. the 2026-08-07
# sam visit) can be carrying: the old client under launchd label
# `com.cograda.comar` (and installed via `pipx install comar` rather than the
# current venv-managed path), an old Syncthing instance under
# `com.cograda.comar.syncthing` still pointed at the old vault path, an old
# working folder at `~/Desktop/Code/comar` (no `.git` — a provisioned folder,
# not a dev checkout, so the git-repo guard below doesn't catch it) and
# `~/.config/comar`. None of the gates/steps below know any of this exists —
# a fresh Syncthing instance on the same REST port (8384) and a fresh launchd
# label (`com.lios.sync`) would simply run *alongside* the old ones rather
# than replacing them, which is a port clash and two daemons fighting over
# EventKit, not a clean install.
#
# `LIOS_CLEAN_SLATE=1` skips the confirmation prompt (used for a scripted
# re-run); otherwise residue triggers a one-line confirm, read from /dev/tty
# since stdin is the piped script itself. Idempotent: a second run finds
# nothing left to retire and prints nothing.
# ──────────────────────────────────────────────────────────────────────────────
OLD_LAUNCHD_LABELS=(
  "com.cograda.comar"
  "com.cograda.comar.syncthing"
  "com.cograda.comar.daemoncheck"
  "com.cograda.comar.env"
)
OLD_WORKING_DIR_CANDIDATES=(
  "${HOME}/Desktop/Code/comar"
  "${HOME}/Comar"
)
OLD_CONFIG_DIR="${HOME}/.config/comar"
OLD_APP_SUPPORT_DIR="${HOME}/Library/Application Support/comar"

_residue_old_working_dir() {
  # Prints the first candidate that exists, has no .git (a provisioned
  # folder, never a dev checkout), and isn't already today's WORKING_DIR.
  local d
  for d in "${OLD_WORKING_DIR_CANDIDATES[@]}"; do
    if [[ -d "${d}" && ! -d "${d}/.git" && "${d}" != "${WORKING_DIR}" ]]; then
      printf '%s' "${d}"
      return 0
    fi
  done
  return 1
}

residue_present() {
  local label
  for label in "${OLD_LAUNCHD_LABELS[@]}"; do
    [[ -f "${HOME}/Library/LaunchAgents/${label}.plist" ]] && return 0
  done
  command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q '^comar ' && return 0
  [[ -d "${OLD_CONFIG_DIR}" ]] && return 0
  [[ -d "${OLD_APP_SUPPORT_DIR}" ]] && return 0
  _residue_old_working_dir >/dev/null && return 0
  return 1
}

gate_clean_slate() {
  residue_present || { ok "No pre-lios residue found"; return; }

  say "Pre-lios residue found on this Mac — retiring it before installing fresh"
  if [[ "${LIOS_CLEAN_SLATE:-}" != "1" ]]; then
    prompt "This will unload old launchd agents, uninstall the old pipx package,"
    prompt "stop the old Syncthing instance, and rename (never delete) the old"
    prompt "working folder aside. Nothing of hers is migrated — continue? [Y/n]"
    local reply="y"
    if [ -r /dev/tty ]; then read -r reply < /dev/tty || reply="y"; fi
    case "${reply}" in
      [nN]*) die "Aborted by user. Re-run with LIOS_CLEAN_SLATE=1 to skip this prompt." ;;
    esac
  fi

  # 1. Old launchd agents — bootout then remove the plist, each independently
  #    tolerant of already being gone (idempotent re-run).
  local label plist
  for label in "${OLD_LAUNCHD_LABELS[@]}"; do
    plist="${HOME}/Library/LaunchAgents/${label}.plist"
    if [[ -f "${plist}" ]]; then
      launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
      launchctl unload "${plist}" 2>/dev/null || true
      rm -f "${plist}"
      ok "Retired old launchd agent ${label}"
    fi
  done
  # Belt-and-braces: a process can outlive its plist if it was started by hand.
  pkill -f "${OLD_APP_SUPPORT_DIR}/bin/syncthing" 2>/dev/null || true

  # 2. Old pipx package. Some machines got the daemon via `pipx install comar`
  #    before the venv-managed install path existed; a stale entry there would
  #    leave a second `comar`/`lios-sync` binary on PATH.
  if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q '^comar '; then
    pipx uninstall comar >/dev/null 2>&1 || true
    ok "Uninstalled old pipx package 'comar'"
  fi

  # 3. Old working folder — renamed aside, never deleted. The server holds
  #    the canonical vault; this is local-only residue, but Time Machine is
  #    its only backup, so it is not this script's place to remove it.
  local old_dir
  if old_dir="$(_residue_old_working_dir)"; then
    local renamed="${old_dir}.pre-lios-$(date -u +%Y%m%d)"
    if [[ ! -e "${renamed}" ]]; then
      mv "${old_dir}" "${renamed}"
      ok "Renamed old working folder aside: ${renamed}"
    else
      warn "${renamed} already exists — leaving ${old_dir} in place"
    fi
  fi

  # 4. Old config dir — nothing copied out of it first. This is a from-scratch
  #    rollout; the server (not this file) is the source of truth for
  #    everything that matters.
  if [[ -d "${OLD_CONFIG_DIR}" ]]; then
    rm -rf "${OLD_CONFIG_DIR}"
    ok "Removed old ${OLD_CONFIG_DIR}"
  fi

  ok "Clean slate complete — proceeding with a fresh lios install"
}

gate_clean_slate

# ──────────────────────────────────────────────────────────────────────────────
# Guard: refuse to install into a development checkout.
# Step 7.5 below unconditionally overwrites CLAUDE.md + .claude/commands in
# WORKING_DIR on every run — that's fine for a provisioned working folder, but
# would clobber hand-written project files if run against a dev clone of the
# comar repo itself. A `.git` directory is the tell.
# ──────────────────────────────────────────────────────────────────────────────
if [[ -d "${WORKING_DIR}/.git" ]]; then
  die "refusing to install into a git repository — this looks like a development checkout, not a provisioned working folder"
fi

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
# The Tailscale Mac app (App Store or standalone .pkg) does NOT put a
# `tailscale` binary on PATH — the CLI lives inside the bundle. `command -v`
# alone therefore reports "not installed" on a Mac that has Tailscale running,
# and the installer went on to download a package it did not need (and the URL
# it used 404s — 2026-09-06, Sam's Mac). Resolve the CLI from either place.
TAILSCALE_BIN=""
tailscale_cli() {
  if [[ -n "${TAILSCALE_BIN}" && -x "${TAILSCALE_BIN}" ]]; then return 0; fi
  if command -v tailscale >/dev/null 2>&1; then TAILSCALE_BIN="$(command -v tailscale)"; return 0; fi
  if [[ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]]; then
    TAILSCALE_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"; return 0
  fi
  return 1
}
tailscale_installed() { tailscale_cli || [[ -d "/Applications/Tailscale.app" ]]; }
tailscale_running() {
  tailscale_cli || return 1
  "${TAILSCALE_BIN}" status --json 2>/dev/null | grep -q '"BackendState": *"Running"' || return 1
  # Heartbeat with no auth should 401 (proves the host is reachable; auth is checked next step).
  local code
  code=$(curl -o /dev/null -s -w "%{http_code}" --max-time 5 "${SERVER}/api/v1/heartbeat" || echo 000)
  [[ "${code}" == "401" || "${code}" == "200" ]]
}
gate_tailscale() {
  if tailscale_running; then ok "Tailscale connected and ${SERVER} reachable"; return; fi
  if ! tailscale_installed; then
    say "Installing Tailscale (downloading .pkg)"
    local tmp
    tmp=$(mktemp -d)
    # Tailscale-latest.pkg (no -macos) is a 404; the -macos name redirects to
    # the current standalone build.
    curl -fsSL https://pkgs.tailscale.com/stable/Tailscale-latest-macos.pkg -o "${tmp}/Tailscale.pkg"
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
# VAULT_DIR/WORKING_DIR are declared up top (needed by the git-repo guard).
# Syncthing's macOS release assets are .zip files (they always have been —
# the tar.gz names this script used until 2026-09-06 never existed, so gate 3
# could not pass on any Mac that did not already have Syncthing). Universal
# build: one asset for arm64 and x86_64. Sync protocol is compatible with the
# server's and with Alex's brew-installed 2.1.x.
SYNCTHING_VERSION="v2.1.3"
SYNCTHING_BIN="${HOME}/Library/Application Support/lios/bin/syncthing"
SYNCTHING_CONFIG="${HOME}/Library/Application Support/Syncthing"
SYNCTHING_PLIST_LABEL="com.lios.sync.syncthing"

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
    local zipname tmp
    zipname="syncthing-macos-universal-${SYNCTHING_VERSION}.zip"
    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/syncthing/syncthing/releases/download/${SYNCTHING_VERSION}/${zipname}" \
         -o "${tmp}/syncthing.zip"
    unzip -q "${tmp}/syncthing.zip" -d "${tmp}"
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
  mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/Library/Logs/lios"
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
  <key>StandardOutPath</key><string>${HOME}/Library/Logs/lios/syncthing.out</string>
  <key>StandardErrorPath</key><string>${HOME}/Library/Logs/lios/syncthing.err</string>
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
                     "${SERVER}/api/v1/syncthing/pair") \
    || die "Pairing call to ${SERVER}/api/v1/syncthing/pair failed"
  echo "  pair response: ${server_resp}"

  server_resp=$(curl -fsS -H "Authorization: Bearer ${TOKEN}" "${SERVER}/api/v1/syncthing/server-id") \
    || die "Could not read the server's Syncthing device id from ${SERVER}"
  server_device_id=$(echo "${server_resp}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["device_id"])')
  local folder_ids
  folder_ids=$(echo "${server_resp}" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["folders"]))')
  say "Server device ID: ${server_device_id:0:15}…, folders: ${folder_ids}"

  # 5. Configure local Syncthing: add server peer, remove default folder
  # Sync host is derived from SERVER rather than hardcoded — strip scheme and
  # any port/path (e.g. "https://example.tail-scale.ts.net:8400/x" -> host).
  local sync_host
  sync_host="$(printf '%s' "${SERVER}" | sed -E 's#^[a-z]+://##; s#[:/].*$##')"
  syncthing_api_call DELETE /rest/config/folders/default >/dev/null || true
  syncthing_api_call PUT "/rest/config/devices/${server_device_id}" \
    "{\"deviceID\":\"${server_device_id}\",\"name\":\"comar-server\",\"addresses\":[\"tcp://${sync_host}:22000\"],\"compression\":\"metadata\",\"autoAcceptFolders\":true}" >/dev/null
  syncthing_api_call PATCH /rest/config/options \
    '{"globalAnnounceEnabled":false,"relaysEnabled":false,"natEnabled":false,"urAccepted":-1}' >/dev/null

  # 6. Pre-create the vault path so Syncthing's auto-accept lands files there
  mkdir -p "${VAULT_DIR}"

  # 7. Add the personal folder share
  local personal_folder="vault-${USER_NAME}"
  syncthing_api_call PUT "/rest/config/folders/${personal_folder}" \
    "{\"id\":\"${personal_folder}\",\"label\":\"${USER_LABEL} Vault\",\"path\":\"${VAULT_DIR}\",\"type\":\"sendreceive\",\"devices\":[{\"deviceID\":\"${device_id}\"},{\"deviceID\":\"${server_device_id}\"}],\"rescanIntervalS\":60,\"fsWatcherEnabled\":true,\"ignorePerms\":true}" >/dev/null
  cat > "${VAULT_DIR}/.stignore" <<STIGN
.obsidian/**
.git/**
.embeddings/**
.DS_Store
STIGN

  ok "Syncthing paired with comar-server; folders ${folder_ids}"

  # 8. Wait for initial sync to settle (each folder either matches server or has files transferring)
  say "Waiting for initial sync to settle (up to 60s)"
  # A folder in an error state reports needFiles=0 too, because it is not
  # scanning at all — on 2026-09-06 "folder marker missing" passed this check
  # as "in sync" with an empty vault on disk. Read the state and error, and
  # stop on an error rather than reassuring. Never "fix" a missing .stfolder
  # by recreating it on an empty directory: Syncthing then scans the empty
  # folder and announces every file as deleted to the server.
  local st pending
  for _ in $(seq 1 12); do
    sleep 5
    st=$(syncthing_api_call GET "/rest/db/status?folder=${personal_folder}" \
         | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("state",""), d.get("needFiles",0), d.get("globalFiles",0), d.get("localFiles",0), d.get("error","") or d.get("invalid","") or "-")')
    set -- ${st}; local state="$1" need="$2" glob="$3" loc="$4"; shift 4; local err="$*"
    if [[ "${state}" == "error" || "${err}" != "-" ]]; then
      die "Syncthing folder ${personal_folder} is in error: ${err}. Not continuing — see the runbook (Syncthing folder errors) before touching ${VAULT_DIR}."
    fi
    # Not loc==glob: files matched by .stignore (.obsidian/**) are counted in
    # the global set but never pulled, so local is legitimately smaller.
    if [[ "${need}" == "0" && "${state}" == "idle" ]]; then
      ok "Vault folder in sync (${loc} files local, ${glob} global incl. ignored)"; break
    fi
    prompt "  ${state}: ${need} pending, ${loc}/${glob} local…"
  done
}

# ──────────────────────────────────────────────────────────────────────────────
# Gate 4: Claude Code app — installed (sign-in deferred to first launch)
# ──────────────────────────────────────────────────────────────────────────────
# Claude Code ships three ways on a Mac and only one of them is a bundle
# literally named "Claude Code.app": the desktop app is "Claude.app" (Claude
# Code lives inside it), and the CLI is a `claude` binary on PATH. Checking for
# the one name hung the installer forever on a Mac that had Claude installed
# (2026-09-06, Sam's Mac — wait_until has no timeout by design).
claude_code_present() {
  [[ -d "/Applications/Claude Code.app" ]] && return 0
  [[ -d "/Applications/Claude.app" ]] && return 0
  command -v claude >/dev/null 2>&1
}
gate_claude_code() {
  if claude_code_present; then
    ok "Claude Code present (app or CLI)"
    return
  fi
  warn "Claude Code not installed."
  prompt "Download from https://claude.ai/download and install."
  prompt "Sign in with your Anthropic account on first launch."
  wait_until "Claude Code installed" claude_code_present
  ok "Claude Code installed"
}

# ──────────────────────────────────────────────────────────────────────────────
# Steps 5–11: comar-specific setup
# ──────────────────────────────────────────────────────────────────────────────

# The lios-sync wheel needs Python >= 3.11 (client/pyproject.toml). Apple's
# Command Line Tools python3 is 3.9, so on a Mac with nothing else installed
# the venv built fine and `pip install` then refused the wheel. Prefer any
# suitable interpreter already present (python.org, Homebrew, PATH); otherwise
# bootstrap one with uv — a single static binary into ~/.local/bin that
# fetches a managed CPython. No sudo, no Homebrew, nothing system-wide.
PY_MIN="3.11"
py_ok() { [[ -x "$1" ]] && "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= (${PY_MIN//./, }) else 1)" 2>/dev/null; }
find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && py_ok "$(command -v "$c")"; then command -v "$c"; return 0; fi
  done
  for c in /Library/Frameworks/Python.framework/Versions/3.1[1-9]/bin/python3 \
           /opt/homebrew/bin/python3.1[1-9] /usr/local/bin/python3.1[1-9]; do
    if py_ok "$c"; then echo "$c"; return 0; fi
  done
  say "No Python >= ${PY_MIN} on this Mac — installing one with uv (user-local, no sudo)" >&2
  if ! command -v uv >/dev/null 2>&1 && [[ ! -x "${HOME}/.local/bin/uv" ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >&2
  fi
  local uv="${HOME}/.local/bin/uv"; command -v uv >/dev/null 2>&1 && uv="$(command -v uv)"
  "${uv}" python install 3.12 >&2
  c="$("${uv}" python find 3.12)"
  py_ok "$c" && echo "$c"
}

# Step 5: venv + wheel
step_install_daemon() {
  local app_support="${HOME}/Library/Application Support/lios"
  local venv="${app_support}/venv"
  mkdir -p "${app_support}"

  if [[ ! -x "${venv}/bin/python3" ]]; then
    local py
    py="$(find_python)" || die "Could not find or install a Python >= ${PY_MIN}"
    say "Creating venv at ${venv} with $("${py}" --version 2>&1)"
    "${py}" -m venv "${venv}"
  else
    "${venv}/bin/python3" -c "import sys; raise SystemExit(0 if sys.version_info >= (${PY_MIN//./, }) else 1)" \
      || die "Existing venv at ${venv} is older than Python ${PY_MIN} — delete it and re-run."
  fi
  [[ -x "${venv}/bin/pip" ]] || "${venv}/bin/python3" -m ensurepip --upgrade >/dev/null

  say "Downloading latest lios-sync wheel"
  local tmp
  tmp=$(mktemp -d)
  # -OJ: keep the server's Content-Disposition filename. pip validates the
  # wheel filename (name-version-pytag-abi-platform.whl) before it looks
  # inside; saving as "lios_sync.whl" was "Invalid wheel filename (wrong
  # number of parts)" — pipx, which the pre-2026-09 installer used, was
  # lenient about this and hid it.
  (cd "${tmp}" && curl -fsSL -OJ -H "Authorization: Bearer ${TOKEN}" "${SERVER}/api/client/download/latest")
  local whl
  whl="$(ls "${tmp}"/*.whl 2>/dev/null | head -1)"
  [[ -n "${whl}" ]] || die "Wheel download produced no .whl in ${tmp}"

  say "Installing $(basename "${whl}") into venv"
  "${venv}/bin/pip" install --quiet --upgrade pip
  "${venv}/bin/pip" install --quiet --force-reinstall "${whl}"
  rm -rf "${tmp}"

  mkdir -p "${HOME}/.local/bin"
  ln -sf "${venv}/bin/lios-sync" "${HOME}/.local/bin/lios-sync"
  ok "lios-sync daemon installed (venv-managed)"
}

# Step 6: config.toml
step_write_config() {
  local cfg_dir="${HOME}/.config/lios"
  local cfg="${cfg_dir}/config.toml"
  mkdir -p "${cfg_dir}"
  if [[ -f "${cfg}" ]]; then
    warn "config.toml already exists — leaving alone. Delete it and re-run to regenerate."
    return
  fi
  cat > "${cfg}" <<EOF
# Generated by the lios installer on $(date -u +%Y-%m-%dT%H:%M:%SZ)
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

# Step 7: working folder at ${WORKING_DIR}/{.mcp.json, CLAUDE.md, vault/}
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
    "lios": {
      "type": "http",
      "url": "${SERVER}/mcp/",
      "headers": {
        "Authorization": "Bearer \${LIOS_TOKEN}"
      }
    }
  }
}
MCP
  fi

  ok "${WORKING_DIR} working folder ready"
}

# Step 7.5: curated command set + CLAUDE.md, rendered per-user by the server
# (sam-rollout Phase B2). Overwrites on every run — this is the one place
# that's meant to stay current without a second visit to the Mac, and the
# installer as a whole is documented as safe to re-run.
step_install_commands() {
  local proj="${WORKING_DIR}"
  mkdir -p "${proj}/.claude/commands"

  local resp
  resp=$(curl -fsS -H "Authorization: Bearer ${TOKEN}" "${SERVER}/api/v1/commands") \
    || { warn "Couldn't fetch /api/v1/commands — leaving CLAUDE.md/.claude/commands as-is"; return; }

  # The response goes via a file, not stdin: `echo | python3 - <<'EOF'` hands
  # stdin to the heredoc (the program), so json.load(sys.stdin) read EOF and
  # this step had never once succeeded (found 2026-09-06 on Sam's Mac).
  local resp_file
  resp_file="$(mktemp)"
  printf '%s' "${resp}" > "${resp_file}"
  python3 - "${proj}" "${resp_file}" <<'PYEOF'
import json, sys, pathlib
proj = pathlib.Path(sys.argv[1])
data = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
(proj / "CLAUDE.md").write_text(data["claude_md"], encoding="utf-8")
cmd_dir = proj / ".claude" / "commands"
cmd_dir.mkdir(parents=True, exist_ok=True)
for filename, content in data["commands"].items():
    (cmd_dir / filename).write_text(content, encoding="utf-8")
PYEOF
  rm -f "${resp_file}"
  ok "CLAUDE.md + curated commands installed from ${SERVER}/api/v1/commands"
}

# Step 8: .zshenv env shim (so GUI-launched Claude Code sees ${LIOS_TOKEN})
step_zshenv() {
  local shim="${HOME}/.config/lios/env.sh"
  cat > "${shim}" <<'EOF'
# lios — exports LIOS_TOKEN (and COMAR_TOKEN, for older .mcp.json files) from ~/.config/lios/config.toml.
# Sourced by ~/.zshenv so GUI-launched apps (Claude Code via Spotlight/Dock)
# see the token, not only interactive terminals.
_comar_config="${HOME}/.config/lios/config.toml"
if [ -r "${_comar_config}" ]; then
  LIOS_TOKEN=$(awk -F'"' '/^[[:space:]]*token[[:space:]]*=/ {print $2; exit}' "${_comar_config}")
  if [ -n "${LIOS_TOKEN}" ]; then
    # .mcp.json reads ${LIOS_TOKEN}; older files read ${COMAR_TOKEN}. Until
    # 2026-09-06 this exported only COMAR_TOKEN — which was never assigned —
    # so GUI-launched Claude Code saw neither.
    COMAR_TOKEN="${LIOS_TOKEN}"
    export LIOS_TOKEN COMAR_TOKEN
  else
    echo "lios: token empty in ~/.config/lios/config.toml — LIOS_TOKEN not set" >&2
  fi
else
  echo "lios: ~/.config/lios/config.toml unreadable — LIOS_TOKEN not set" >&2
fi
unset _comar_config
EOF
  local zshenv="${HOME}/.zshenv"
  local line='[ -r "$HOME/.config/lios/env.sh" ] && . "$HOME/.config/lios/env.sh"'
  if [[ -f "${zshenv}" ]] && grep -Fq "${line}" "${zshenv}"; then
    ok ".zshenv already sources env shim"
  else
    {
      [[ -f "${zshenv}" ]] && echo
      echo "# lios — token export for GUI-launched tools."
      echo "${line}"
    } >> "${zshenv}"
    ok ".zshenv patched"
  fi
}

# Step 9: launchd (run daemon at login, restart on crash)
step_launchd() {
  local label="com.lios.sync"
  local plist="${HOME}/Library/LaunchAgents/${label}.plist"
  local comar_bin="${HOME}/Library/Application Support/lios/venv/bin/lios-sync"
  local log_dir="${HOME}/Library/Logs/lios"
  mkdir -p "${log_dir}" "${HOME}/Library/LaunchAgents"

  cat > "${plist}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key><array>
    <string>${comar_bin}</string><string>daemon</string>
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

# Step 9.5: daemon split-brain check — the daemon's process can stay alive
# while its EventKit write channel is dead, so reminders_add/complete silently
# queue and never execute. Ships scripts/lios-sync-check.sh's logic as a
# standalone file (the repo it lives in is never present on Sam's machine)
# and schedules it via launchd every 30 minutes; it self-heals via
# `launchctl kickstart -k` when the daemon looks sick.
step_daemon_check() {
  local bin_dir="${HOME}/Library/Application Support/lios/bin"
  local check_script="${bin_dir}/lios-sync-check"
  local log_dir="${HOME}/Library/Logs/lios"
  mkdir -p "${bin_dir}" "${log_dir}"

  cat > "${check_script}" <<'DCEOF'
#!/usr/bin/env bash
# lios-sync-check — preflight liveness check for the lios-sync daemon.
#
# Why this exists: the daemon can enter a "split-brain" state where the process
# is alive (launchctl shows a PID) but its EventKit write channel is dead — so
# reminders_complete / reminders_add silently queue with "no client connected"
# and never execute. A plain process check misses this. The daemon's /health
# endpoint exposes per-task liveness (server_connected, last_error, finished),
# which is what we actually assert here.
#
# Behaviour:
#   - Healthy            -> exit 0, print one green line.
#   - Unreachable / sick -> auto-heal via `launchctl kickstart -k`, re-check.
#   - Still sick after heal -> exit 1 (caller should not rely on reminders).
set -uo pipefail

LABEL="com.lios.sync"
PORT="${LIOS_SYNC_PORT:-${COMAR_MCP_PORT:-9400}}"
URL="http://localhost:${PORT}/health"
HEAL=1
[ "${1:-}" = "--no-heal" ] && HEAL=0

# Print PASS/FAIL verdict from a /health JSON body passed as $1.
# Exit 0 if healthy, 1 otherwise. Reasons go to stderr.
# NB: the JSON is handed to python via an env var, NOT stdin — stdin is taken
# by the heredoc program source, so json.load(sys.stdin) would read nothing.
assess() {
  HEALTH_JSON="${1:-}" python3 <<'PY'
import json, os, sys
raw = os.environ.get("HEALTH_JSON", "")
try:
    h = json.loads(raw)
except Exception as e:
    print(f"unreachable / non-JSON ({e})", file=sys.stderr); sys.exit(1)

problems = []
if h.get("status") != "ok":
    problems.append(f"status={h.get('status')!r}")
if not h.get("server_connected"):
    problems.append("server_connected=false")
if not h.get("vault_watcher_active"):
    problems.append("vault_watcher_active=false")
depth = h.get("retry_queue_depth", 0) or 0
if depth > 0:
    problems.append(f"retry_queue_depth={depth}")

# Per-task health — this is the split-brain detector.
for name, t in (h.get("tasks") or {}).items():
    if t.get("finished"):
        problems.append(f"task[{name}].finished")
    if t.get("last_error"):
        problems.append(f"task[{name}].last_error={t['last_error']!r}")

rem = (h.get("tasks") or {}).get("reminders") or {}
alive = rem.get("alive_seconds_ago")
note = f"reminders alive {alive}s ago" if alive is not None else "reminders alive n/a"

if problems:
    print("; ".join(problems), file=sys.stderr); sys.exit(1)
print(f"ok — server_connected, vault_watcher_active, retry_queue=0, {note}")
sys.exit(0)
PY
}

check() {
  local body
  body="$(curl -s -m 3 "$URL" 2>/dev/null || true)"
  assess "$body"
}

# Tolerant check: require 3 consecutive failures (~6s) before declaring the
# daemon unhealthy, so a single transient empty poll never triggers a needless
# restart of a daemon that's actually fine.
check_stable() {
  local last="" i
  for i in 1 2 3; do
    if last="$(check 2>/tmp/.comar_health_err)"; then echo "$last"; return 0; fi
    sleep 2
  done
  cat /tmp/.comar_health_err >&2 2>/dev/null
  return 1
}

if msg="$(check_stable)"; then
  echo "lios-sync daemon healthy — $msg"
  exit 0
fi

echo "lios-sync daemon unhealthy: $(cat /tmp/.comar_health_err 2>/dev/null)" >&2

if [ "$HEAL" -eq 0 ]; then
  echo "   (--no-heal set; not restarting)" >&2
  exit 1
fi

echo "   kicking daemon (launchctl kickstart -k gui/$(id -u)/${LABEL})…" >&2
launchctl kickstart -k "gui/$(id -u)/${LABEL}" >/dev/null 2>&1

# Give launchd + SSE reconnect time to settle. A hard kill means the daemon
# must rebind :9400 and re-establish the MCP proxy + SSE event stream to the
# server, which takes ~15-30s — poll patiently rather than declaring failure early.
for i in $(seq 1 30); do
  sleep 1
  if msg="$(check)"; then
    echo "lios-sync daemon healthy after restart (~${i}s) — $msg"
    exit 0
  fi
done

echo "lios-sync daemon still unhealthy after kickstart: $(cat /tmp/.comar_health_err 2>/dev/null)" >&2
echo "   Investigate: launchctl print gui/$(id -u)/${LABEL} | grep -A3 'last exit'" >&2
exit 1
DCEOF
  chmod +x "${check_script}"
  ok "Daemon split-brain check written to ${check_script}"

  local dc_label="com.lios.sync.daemoncheck"
  local dc_plist="${HOME}/Library/LaunchAgents/${dc_label}.plist"
  cat > "${dc_plist}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${dc_label}</string>
  <key>ProgramArguments</key><array>
    <string>${check_script}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>1800</integer>
  <key>StandardOutPath</key><string>${log_dir}/daemoncheck.out</string>
  <key>StandardErrorPath</key><string>${log_dir}/daemoncheck.err</string>
</dict>
</plist>
EOF
  launchctl unload "${dc_plist}" 2>/dev/null || true
  launchctl load "${dc_plist}"
  ok "Daemon split-brain check scheduled every 30 min (${dc_label})"
}

# Step 9.6: macOS permission prompts the daemon needs.
# Reminders (EventKit): ReminderStore._init_store() calls
# EKEventStore.requestAccessToEntityType_completion_ the moment the daemon
# process starts (client/src/lios_sync/eventkit.py) — launchd starts it with
# ProcessType=Interactive so the native dialog reaches the logged-in user's
# screen, but it blocks for up to 30s and is easy to miss behind other windows.
# Poll /health for the reminders task going alive as the signal that Allow was
# clicked (health_server.py's `tasks` comes from TaskSupervisor.health(),
# same shape scripts/lios-sync-check reads).
#
# Full Disk Access: NOT required for this install. It's only needed by the
# opt-in Voice Memos watcher (client/src/lios_sync/voice_memos.py, reads the
# group.com.apple.VoiceMemos.shared container) — off by default, and this
# installer never enables it. It's also not needed for the vault watcher:
# WORKING_DIR is ${HOME}/lios, directly under the home directory, not under
# the FDA-protected Desktop/Documents/Downloads — unlike the pre-Phase-0
# `~/Desktop/Code/comar` path this replaces.
step_permissions_check() {
  local port
  port="${LIOS_SYNC_PORT:-9400}"
  say "Waiting for Reminders access to be granted (a system dialog should have appeared)"
  local i
  for i in $(seq 1 12); do
    local body alive
    # `|| true`: under set -e a refused connection (daemon still restarting
    # after the launchd reload one step earlier) exits the whole script here,
    # silently. That is where every 2026-09-06 run ended.
    body="$(curl -s -m 3 "http://localhost:${port}/health" 2>/dev/null || true)"
    alive=$(echo "${body}" | python3 -c 'import json,sys
try:
    h=json.load(sys.stdin)
    r=(h.get("tasks") or {}).get("reminders") or {}
    print(r.get("alive_seconds_ago") if r.get("alive_seconds_ago") is not None else "")
except Exception:
    print("")' 2>/dev/null)
    if [[ -n "${alive}" ]]; then
      ok "Reminders access granted (daemon's reminders task is alive)"
      return
    fi
    if (( i == 1 )); then
      prompt "If a 'lios-sync would like to access your reminders' dialog appeared, click Allow."
      prompt "If nothing appeared, open System Settings → Privacy & Security → Reminders and enable it there."
    fi
    sleep 5
  done
  warn "Couldn't confirm Reminders access after 60s — reminders_add/complete will queue silently until it's granted."
  warn "Check: System Settings → Privacy & Security → Reminders."
}

# Step 10: heartbeat 200
step_heartbeat() {
  local code
  code=$(curl -o /dev/null -s -w "%{http_code}" --max-time 10 \
              -H "Authorization: Bearer ${TOKEN}" \
              "${SERVER}/api/v1/heartbeat" || echo 000)
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
step_install_commands
step_zshenv
step_launchd
step_permissions_check
step_daemon_check
step_heartbeat
step_smoke

echo
ok "lios is installed end-to-end for ${USER_NAME}."
prompt "Next steps:"
prompt "  1. Open Claude Code, sign in if you haven't already."
prompt "  2. File → Open Folder → ${WORKING_DIR}"
prompt "  3. Ask: 'what's on my calendar today?' to confirm it works."
prompt "To re-run this installer later (after a fix, or to repair) without a new code:"
prompt "  curl -fsSL -H \"Authorization: Bearer \$LIOS_TOKEN\" ${SERVER}/api/install/rerun | bash"
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


def _public_server_url() -> str:
    # Prefer the public/Tailscale URL: that's what a bootstrap reaches us on.
    return getattr(settings, "server_public_url", None) or "https://ubuntudockerbox.tail78010b.ts.net"


@router.get("/rerun", response_class=PlainTextResponse)
async def rerun_install_script(
    request: Request, user: User = Depends(get_current_user),
) -> PlainTextResponse:
    """Re-render the installer for a machine that already holds a bearer.

    Every install-code fetch is single-use, so until 2026-09-06 re-running the
    script after a fix meant: deploy, mint a new code on the server, carry the
    one-liner to the other Mac, run it. Sam's from-scratch install hit that
    loop three times in one evening. Once step 6 has written the device's
    bearer to disk, this route lets the same machine fetch the current script
    with the token it already has — no code, no minting, nothing to carry.

    The bearer IS the auth (validated by `get_current_user`, same failure
    budget as every other bearer path), and the script is rendered for that
    token's own user, so a re-run can only reinstall the caller as themselves.
    Declared before `/{code}` so "rerun" is never read as a code.
    """
    authz = request.headers.get("authorization", "")
    token = authz.split(" ", 1)[1].strip() if " " in authz else ""
    label = "rerun"
    db = get_db()
    with db.session() as session:
        row = session.execute(
            select(ClientToken).where(ClientToken.token_hash == hash_token(token), ClientToken.is_active.is_(True))
        ).scalar_one_or_none()
        if row is not None:
            label = row.label
    return PlainTextResponse(
        _render(_public_server_url(), token, user.name, label),
        media_type="text/x-shellscript",
    )


@router.get("/{code}", response_class=PlainTextResponse)
async def fetch_install_script(code: str, request: Request) -> PlainTextResponse:
    """Render and return the install script for a single-use code.

    Marks the code redeemed on success (with timestamp + caller IP). Subsequent
    fetches return 410 Gone. Expired codes return 410 Gone with a different
    detail. Unknown codes return 404.

    F-security: this route is deliberately in `AUTH_EXEMPT_PREFIXES` (a new
    machine has no bearer yet) and the code itself is the auth boundary —
    which means a guessed/enumerated code is exactly as sensitive as a
    guessed bearer, and had no rate limit at all. Same per-IP failure
    budget as the bearer paths (`app/auth/rate_limit.py`), checked before
    the DB lookup: a miss (unknown code) or a redeemed/expired/broken code
    spends budget the same way a wrong bearer does; a genuine hit does not.
    Brute-force budget: `secrets.token_urlsafe(16)` (`create_install_code.py`)
    is 128 bits of entropy over a 64-symbol alphabet — MAX_ATTEMPTS_PER_WINDOW
    (20) failures per WINDOW_SECONDS (10s) per IP caps a guessing script at
    ~120/minute against a 2^128 code space, i.e. this closes the "no rate
    limit at all" hole, not a "brute-forceable in practice" one.
    """
    client_ip = request.client.host if request.client else "unknown"
    if is_over_limit(client_ip):
        return JSONResponse({"error": "Too many attempts"}, status_code=429)

    db = get_db()
    with db.session() as session:
        ic = session.execute(
            select(InstallCode).where(InstallCode.code == code)
        ).scalar_one_or_none()

        if ic is None:
            record_failure(client_ip)
            raise HTTPException(404, "Unknown install code")

        now = datetime.now(timezone.utc)
        if ic.redeemed_at is not None:
            record_failure(client_ip)
            raise HTTPException(410, "Install code already redeemed")
        if ic.expires_at <= now:
            record_failure(client_ip)
            raise HTTPException(410, "Install code expired")

        token = session.get(ClientToken, ic.token_id)
        if token is None or not token.is_active:
            record_failure(client_ip)
            raise HTTPException(409, "Backing token missing or revoked")
        if not ic.token_plaintext:
            # Should be unreachable (redeemed_at gate above already covers
            # single-use) — defensive in case a row was ever hand-edited.
            record_failure(client_ip)
            raise HTTPException(409, "Install token no longer available")

        # FK guarantees the User row exists (RESTRICT). Snapshot attrs before
        # commit — session is expire_on_commit=True per server CLAUDE.md.
        from app.models.users import User  # local: avoids circular import
        user_row = session.get(User, ic.user_id)
        if user_row is None or not user_row.is_active:
            raise HTTPException(409, "User missing or inactive")
        # F4: token_plaintext is Fernet-encrypted at rest; decrypt only now,
        # right before handing it to the device over the response body.
        token_str = decrypt_token(ic.token_plaintext)
        user_name = user_row.name

        ic.redeemed_at = now
        ic.redeemed_from_ip = (request.client.host if request.client else None) or ""
        # Single-use — the plaintext has now been handed to the device over
        # the response body; it has no reason to keep living in the DB.
        ic.token_plaintext = None
        session.commit()

        logger.info(
            "Install code redeemed: code=%s user=%s label=%s ip=%s",
            code, user_name, ic.label, ic.redeemed_from_ip,
        )

        from app.auth.hashing import token_last4
        from app.services.auth_events import record_auth_event

        record_auth_event(
            outcome="issued",
            token_last4=token_last4(token_str),
            source_ip=ic.redeemed_from_ip or None,
            transport="http",
            user_id=ic.user_id,
        )

        # Server URL the script will phone home to. Prefer the public/Tailscale
        # URL since that's what a non-LAN bootstrap reaches us on. Falls back
        # to comar.lab for LAN re-runs.
        script = _render(_public_server_url(), token_str, user_name, ic.label)
        return PlainTextResponse(script, media_type="text/x-shellscript")
