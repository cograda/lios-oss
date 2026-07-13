#!/usr/bin/env bash
# comar-daemon-check.sh — preflight liveness check for the comar-client daemon.
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
#
# Usage:
#   scripts/comar-daemon-check.sh           # check, auto-heal if needed
#   scripts/comar-daemon-check.sh --no-heal # check only, never restart
#
set -uo pipefail

LABEL="com.cograda.comar"
PORT="${COMAR_MCP_PORT:-9400}"
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
  body="$(curl -s -m 3 "$URL" 2>/dev/null)"
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
  echo "✅ comar daemon healthy — $msg"
  exit 0
fi

echo "⚠️  comar daemon unhealthy: $(cat /tmp/.comar_health_err 2>/dev/null)" >&2

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
    echo "✅ comar daemon healthy after restart (~${i}s) — $msg"
    exit 0
  fi
done

echo "❌ comar daemon still unhealthy after kickstart: $(cat /tmp/.comar_health_err 2>/dev/null)" >&2
echo "   Investigate: launchctl print gui/$(id -u)/${LABEL} | grep -A3 'last exit'" >&2
exit 1
