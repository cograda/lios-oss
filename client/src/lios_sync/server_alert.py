"""Server-silent detector — the off-box half of household liveness.

Every monitor the household has (Pulse, uptime-kuma, Home Assistant, lios
core's own `system_alerts`) runs ON the Proxmox cluster. If the cluster or its
docker host dies, every one of those goes with it — nothing left standing can
push an alert. The only always-on things off the cluster are the family Macs
running this daemon, so the daemon has to be able to notice "the server has
gone quiet" and say so itself, locally, with no server in the loop at all.

This module is pure logic — no daemon wiring, no macOS notification calls
baked into it beyond the small `notify_macos` helper at the bottom, so the
state machine is unit-testable with a fake clock and a fake notifier (see
`tests/test_server_alert.py`).

Three distinct failures, three distinct messages
-------------------------------------------------
"The server is unreachable" is not one condition. Three different things can
make a heartbeat fail, and conflating them produces a message that lies about
what's wrong:

- **Server silent** — heartbeats are failing, this Mac's own network is fine
  (it can reach a well-known external host), and the failure isn't a rejected
  token. This is the one this module exists to catch.
- **Auth invalid** (401 / `AuthError`) — the token was rejected. The server
  may be perfectly healthy; only this Mac's credential is bad.
- **Network down** — this Mac itself cannot reach anything, so the server's
  state is simply unknown, not silent.

Only the first counts as a "server silent" alert. The other two get their own
single notification so nobody is told the server is down when actually it's
this laptop that lost Wi-Fi, or this Mac's token that expired.

State machine
-------------
`ServerSilenceMonitor.record_result(exc)` is called after every heartbeat
attempt (`exc=None` on success). It tracks which of the four states
(`ok` / `server_silent` / `auth_invalid` / `network_down`) currently holds,
when the current non-ok state began, and when it last actually notified —
persisted to disk (`STATE_PATH`) so a daemon restart mid-outage does not
re-fire the notification the moment it comes back up.

- `server_silent` only notifies once `alerts.server_silent_minutes` has
  elapsed since the failures started (0 disables the whole detector).
- Any notified bad state re-fires no more than once per
  `alerts.server_silent_refire_hours` while it persists.
- `auth_invalid` and `network_down` notify as soon as they're detected (they
  are unambiguous the instant they're seen), then follow the same refire rule.
- Recovery (`exc=None` after a notified bad state) fires exactly one "back"
  notification.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_SERVER_SILENT = "server_silent"
STATE_AUTH_INVALID = "auth_invalid"
STATE_NETWORK_DOWN = "network_down"

STATE_PATH = Path.home() / ".config" / "lios" / "server_alert_state.json"

# Well-known, cheap-to-reach host used to tell "my network is down" apart from
# "the server specifically is unreachable". A raw TCP connect, not a full
# HTTP round trip or DNS-dependent hostname — Cloudflare's 1.1.1.1 answers on
# 443 from anywhere with a route to the internet, tailnet or otherwise.
DEFAULT_NETWORK_CHECK_HOST = "1.1.1.1"
DEFAULT_NETWORK_CHECK_PORT = 443
DEFAULT_NETWORK_CHECK_TIMEOUT = 2.0


def check_own_network(
    host: str = DEFAULT_NETWORK_CHECK_HOST,
    port: int = DEFAULT_NETWORK_CHECK_PORT,
    timeout: float = DEFAULT_NETWORK_CHECK_TIMEOUT,
) -> bool:
    """True if this Mac can reach *something* on the internet right now.

    A cheap TCP connect, not a request — this only needs to distinguish "my
    network is up" from "my network is down", not verify any particular
    service.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_MESSAGES = {
    STATE_AUTH_INVALID: (
        "lios",
        "This Mac's server login was rejected (token expired?) — the server "
        "itself may be fine. Ask Alex for a new install code.",
    ),
    STATE_NETWORK_DOWN: (
        "lios",
        "This Mac's own network appears to be down — can't reach the "
        "internet at all, so the server's state is unknown, not necessarily down.",
    ),
    "recovered": (
        "lios",
        "Server connection is back.",
    ),
}


def _server_silent_message(threshold_minutes: int) -> tuple[str, str]:
    return (
        "lios",
        f"The lios server has been unreachable for over {threshold_minutes} "
        "minutes. Every other household monitor runs on that same "
        "infrastructure, so nothing else can raise this.",
    )


@dataclass
class _AlertState:
    current: str = STATE_OK
    since: float | None = None
    last_alert_at: float | None = None
    last_alert_state: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "_AlertState":
        return cls(
            current=data.get("current", STATE_OK),
            since=data.get("since"),
            last_alert_at=data.get("last_alert_at"),
            last_alert_state=data.get("last_alert_state"),
        )


class ServerSilenceMonitor:
    """Tracks server reachability across heartbeats and raises local alerts.

    Not thread-safe by assumption of concurrent callers — the daemon's
    heartbeat loop is the only caller, serially, once per cycle. A lock still
    guards the persisted-state read/write so `state()` can be called from the
    health endpoint's request handler concurrently without racing a write.
    """

    def __init__(
        self,
        *,
        threshold_minutes: int = 20,
        refire_hours: float = 6,
        notifier=None,
        network_check=None,
        clock=None,
        state_path: Path = STATE_PATH,
    ):
        self.threshold_minutes = threshold_minutes
        self.refire_hours = refire_hours
        self.notifier = notifier or notify_macos
        self.network_check = network_check or check_own_network
        self.clock = clock or time.time
        self.state_path = state_path
        self._lock = threading.Lock()
        self._state = self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> _AlertState:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return _AlertState.from_json(data)
        except (OSError, json.JSONDecodeError):
            logger.warning("server_alert state at %s is unreadable — starting fresh", self.state_path)
        return _AlertState()

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self._state.to_json()), encoding="utf-8",
            )
        except OSError:
            logger.warning("Failed to persist server_alert state to %s", self.state_path)

    # -- public API -------------------------------------------------------------

    def classify(self, exc: Exception) -> str:
        """Which of the three failure kinds `exc` represents.

        Imported lazily to avoid a hard import-time dependency on
        `server_client` from this otherwise-standalone module.
        """
        from lios_sync.server_client import AuthError

        if isinstance(exc, AuthError):
            return STATE_AUTH_INVALID
        if not self.network_check():
            return STATE_NETWORK_DOWN
        return STATE_SERVER_SILENT

    def record_result(self, exc: Exception | None) -> str:
        """Call after every heartbeat attempt. Returns the resulting state.

        `exc=None` means the heartbeat succeeded.
        """
        now = self.clock()
        with self._lock:
            if exc is None:
                self._on_success(now)
                return STATE_OK
            kind = self.classify(exc)
            self._on_failure(now, kind)
            return kind

    def state(self) -> dict:
        """Snapshot for `/health` — `lios-sync status` reads this."""
        with self._lock:
            return {
                "state": self._state.current,
                "since": self._state.since,
                "last_alert_at": self._state.last_alert_at,
                "last_alert_state": self._state.last_alert_state,
                "threshold_minutes": self.threshold_minutes,
                "refire_hours": self.refire_hours,
            }

    # -- internals --------------------------------------------------------------

    def _on_failure(self, now: float, kind: str) -> None:
        if self._state.current != kind:
            self._state.current = kind
            self._state.since = now

        if kind == STATE_SERVER_SILENT:
            if self.threshold_minutes <= 0:
                self._save()
                return  # disabled
            elapsed_minutes = (now - (self._state.since or now)) / 60.0
            if elapsed_minutes < self.threshold_minutes:
                self._save()
                return
            self._maybe_notify(kind, now, _server_silent_message(self.threshold_minutes))
        else:
            self._maybe_notify(kind, now, _MESSAGES[kind])
        self._save()

    def _on_success(self, now: float) -> None:
        was_bad = self._state.current != STATE_OK
        previously_notified = (
            self._state.last_alert_state is not None
            and self._state.last_alert_state != STATE_OK
        )
        if was_bad and previously_notified:
            self.notifier(*_MESSAGES["recovered"])
            self._state.last_alert_at = now
            self._state.last_alert_state = STATE_OK
        self._state.current = STATE_OK
        self._state.since = None
        self._save()

    def _maybe_notify(self, kind: str, now: float, message: tuple[str, str]) -> None:
        refire_seconds = self.refire_hours * 3600.0
        if self._state.last_alert_state != kind:
            should_notify = True
        else:
            should_notify = (
                self._state.last_alert_at is None
                or (now - self._state.last_alert_at) >= refire_seconds
            )
        if not should_notify:
            return
        self.notifier(*message)
        self._state.last_alert_at = now
        self._state.last_alert_state = kind


# -- macOS notification -------------------------------------------------------

def _applescript_quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def notify_macos(title: str, message: str) -> None:
    """Raise ONE local macOS notification via the Apple-signed `osascript`.

    Spawning `/usr/bin/osascript` (not a bare `osascript` off PATH) rather
    than any Python notification library keeps this dependency-free and gets
    the notification signed by an Apple binary — see the Scrobbler project
    notes on why an ad-hoc-signed caller can be silently gated on modern
    macOS. Best-effort: a notification failure must never take the daemon
    down or block the heartbeat loop.
    """
    script = (
        f"display notification {_applescript_quote(message)} "
        f"with title {_applescript_quote(title)}"
    )
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            timeout=10,
            capture_output=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to raise macOS notification: %s", title)
