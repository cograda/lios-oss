#!/usr/bin/env bash
# scripts/setup-machine.sh — bootstrap a fresh Mac for comar.
#
# Idempotent: re-running it on a configured machine is a no-op except where
# you've explicitly changed something. Designed to be the single command a
# new machine needs after `git clone`:
#
#   bash scripts/setup-machine.sh
#
# What it does:
#   1. Checks for pipx (installs via brew if missing).
#   2. Installs / upgrades the comar daemon from ./client.
#   3. Runs `comar setup --noninteractive` if ~/.config/comar/config.toml
#      doesn't exist (prompting for the per-user bearer token).
#   4. Writes ~/.config/comar/env.sh — exports COMAR_TOKEN from config.toml.
#   5. Patches ~/.zshenv to source env.sh on every zsh invocation (so
#      GUI-launched apps like Claude Code see the token, not just terminals).
#   6. Verifies with a heartbeat to the server.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_dir="${HOME}/.config/comar"
config_file="${config_dir}/config.toml"
env_shim="${config_dir}/env.sh"
zshenv="${HOME}/.zshenv"

say()  { printf "\033[1;36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }

# 0. python3 ≥3.11 (tomllib) ---------------------------------------------------
# config.toml values are extracted with python3's stdlib tomllib (3.11+),
# which handles real TOML instead of brittle awk field-splitting.
command -v python3 >/dev/null 2>&1 \
  || die "python3 not found — install it (e.g. 'brew install python3'); needed to parse ~/.config/comar/config.toml"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python3 is older than 3.11 (no tomllib) — upgrade it (e.g. 'brew install python3')"
ok "python3 with tomllib available"

# 1. pipx ----------------------------------------------------------------------
if ! command -v pipx >/dev/null 2>&1; then
  say "pipx not found — installing via Homebrew"
  command -v brew >/dev/null 2>&1 || die "Homebrew not installed; install from https://brew.sh first"
  brew install pipx
  pipx ensurepath
fi
ok "pipx available"

# 2. comar daemon --------------------------------------------------------------
say "Installing comar daemon from ${repo_root}/client"
pipx install --force "${repo_root}/client" >/dev/null
ok "daemon installed at $(command -v comar)"

# 3. config.toml ---------------------------------------------------------------
mkdir -p "${config_dir}"
if [ -f "${config_file}" ]; then
  ok "config exists at ${config_file} — leaving alone"
else
  say "No config found — running interactive setup"
  printf "  User (alex/sam): "; read -r user
  printf "  Server URL [http://192.168.1.50:8400]: "; read -r server
  server="${server:-http://192.168.1.50:8400}"
  printf "  Bearer token (from server's client_tokens table): "; read -rs token; echo
  [ -n "${token}" ] || die "token cannot be empty"
  comar setup --noninteractive --user "${user}" --token "${token}" --server "${server}"
fi

# 4. env shim ------------------------------------------------------------------
say "Writing env shim to ${env_shim}"
cat > "${env_shim}" <<'SHIM'
# comar — exports environment variables from ~/.config/comar/config.toml.
# Sourced by ~/.zshenv so GUI-launched apps (Claude Code via Spotlight/Dock)
# see COMAR_TOKEN, not only interactive terminals.
_comar_config="${HOME}/.config/comar/config.toml"
if [ -r "${_comar_config}" ] && command -v python3 >/dev/null 2>&1; then
  COMAR_TOKEN=$(python3 -c 'import tomllib,sys; d=tomllib.load(open(sys.argv[1],"rb")); print(d.get("server",{}).get("token",""))' "${_comar_config}" 2>/dev/null)
  if [ -n "${COMAR_TOKEN}" ]; then
    export COMAR_TOKEN
  fi
fi
unset _comar_config
SHIM
ok "env shim written"

# 5. .zshenv patch -------------------------------------------------------------
zshenv_line='[ -r "$HOME/.config/comar/env.sh" ] && . "$HOME/.config/comar/env.sh"'
if [ -f "${zshenv}" ] && grep -Fq "${zshenv_line}" "${zshenv}"; then
  ok ".zshenv already sources env shim"
else
  say "Appending env shim source line to ${zshenv}"
  {
    [ -f "${zshenv}" ] && echo
    echo "# comar — token export for GUI-launched tools."
    echo "${zshenv_line}"
  } >> "${zshenv}"
  ok ".zshenv patched"
fi

# 6. verify --------------------------------------------------------------------
say "Verifying with server heartbeat"
. "${env_shim}"
[ -n "${COMAR_TOKEN:-}" ] || die "COMAR_TOKEN still empty after sourcing env shim"
server_url=$(python3 -c 'import tomllib,sys; d=tomllib.load(open(sys.argv[1],"rb")); print(d.get("server",{}).get("url",""))' "${config_file}" 2>/dev/null)
[ -n "${server_url}" ] || server_url="http://192.168.1.50:8400"
status=$(curl -sS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${COMAR_TOKEN}" "${server_url%/}/api/v1/heartbeat" --max-time 5 || echo "000")
case "${status}" in
  200) ok "heartbeat 200 from ${server_url}" ;;
  401) die "heartbeat 401 — token rejected. Check the value in ${config_file}." ;;
  000) warn "could not reach ${server_url} — check Wi-Fi / Tailscale, then re-run" ;;
  *)   warn "heartbeat returned HTTP ${status} from ${server_url}" ;;
esac

echo
ok "Setup complete. Quit and relaunch Claude Code so it picks up COMAR_TOKEN."
