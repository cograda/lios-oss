"""In-process per-IP rate limiter for the auth boundary (F8).

Every failed bearer/UI-token check writes an `auth_events` row (audit trail,
V4 chunk 2.5) and, for the bearer paths, still does a `client_tokens` lookup
even when the token turns out to be garbage. A script guessing tokens can
drive both at a rate limited only by network throughput — the `auth_events`
table then grows until the 3am prune job runs.

This is a tailnet-only, single-process service (see server/CLAUDE.md's
transport-stance note), so a bounded in-memory sliding window is enough —
no Redis, no new dependency, no cross-process coordination.

Only FAILED auth attempts spend budget. Every MCP tool call authenticates,
and the household's normal workflow fans parallel subagents out over the
MCP tools — a burst of legitimate, successfully-authenticated traffic must
never 429 itself. So the split is: `is_over_limit(ip)` is checked at each
of the three entry points (`app/auth/client_token.py::get_current_user`,
`app/mcp/server.py::_authenticate_request`, `app/routes/auth.py::login`)
BEFORE any token parsing/DB lookup/`auth_events` insert, and
`record_failure(ip)` is called on each 401 path. A token-guessing script
throttles itself with its own failures; a valid caller never does.

Not a defense against a distributed attack (every source IP gets its own
independent budget) — that's not the threat model for a home server behind
Tailscale/LAN; it's a backstop against one noisy source hammering the auth
path.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

# Sliding window: at most MAX_ATTEMPTS_PER_WINDOW FAILED auth attempts from
# one IP per WINDOW_SECONDS. Failures-only means the ceiling can be tight:
# no legitimate caller fails 20 times in 10 seconds (a daemon with a stale
# token fails once per 5-min heartbeat), while a guessing script is capped
# to ~120 guesses a minute instead of thousands.
WINDOW_SECONDS = 10.0
MAX_ATTEMPTS_PER_WINDOW = 20

# Bound how many distinct IPs we track at once, so a flood of spoofed source
# IPs can't grow this dict without limit. Pruned opportunistically (below)
# rather than on every call, to keep the common case cheap.
_MAX_TRACKED_IPS = 10_000
_PRUNE_EVERY_N_CALLS = 500

_lock = Lock()
_attempts: dict[str, deque[float]] = {}
_calls_since_prune = 0


def _prune_stale(now: float) -> None:
    """Drop any IP whose window has fully expired. Caller holds `_lock`."""
    stale = [
        ip for ip, dq in _attempts.items()
        if not dq or now - dq[-1] > WINDOW_SECONDS
    ]
    for ip in stale:
        del _attempts[ip]


def reset() -> None:
    """Clear all tracked state. Test-only."""
    with _lock:
        _attempts.clear()


def is_over_limit(ip: str, now: float | None = None) -> bool:
    """True if `ip` has spent its failure budget and should be 429'd.

    Call this BEFORE any token parsing, DB lookup, or `auth_events` write on
    the auth path — the whole point is that an over-limit caller never
    reaches any of them. Read-only apart from expiring old entries: checking
    never spends budget, so legitimate traffic at any volume passes freely.
    """
    if now is None:
        now = time.monotonic()

    with _lock:
        dq = _attempts.get(ip)
        if dq is None:
            return False
        while dq and now - dq[0] > WINDOW_SECONDS:
            dq.popleft()
        return len(dq) >= MAX_ATTEMPTS_PER_WINDOW


def record_failure(ip: str, now: float | None = None) -> None:
    """Record one FAILED auth attempt from `ip` against its budget.

    Call on every 401 path (missing/invalid/expired token), right where the
    `auth_events` failure row is written.
    """
    global _calls_since_prune

    if now is None:
        now = time.monotonic()

    with _lock:
        _calls_since_prune += 1
        if _calls_since_prune >= _PRUNE_EVERY_N_CALLS:
            _prune_stale(now)
            _calls_since_prune = 0

        dq = _attempts.get(ip)
        if dq is None:
            if len(_attempts) >= _MAX_TRACKED_IPS:
                _prune_stale(now)
            dq = deque()
            _attempts[ip] = dq

        while dq and now - dq[0] > WINDOW_SECONDS:
            dq.popleft()

        dq.append(now)
