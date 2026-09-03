"""HTTP server client — replaces the old gRPC `ComarClient`.

All comar-server communication goes through this module: tool listing
+ invocation, vault file push, reminders push, log shipping, and the
SSE event stream. Uses `httpx` (sync API for the push paths called
from threads, async client for SSE).

Connection model:
- An *ordered* list of candidate base URLs (`config.server.urls`, e.g.
  `["http://192.168.1.50:8400", "https://box.tailnet.ts.net"]`). The first
  one that answers an authenticated `/api/v1/heartbeat` is used; see
  `comar.endpoints` for why the probe has to be authenticated rather than
  a plain reachability check. Single-URL configs skip the probe entirely,
  so nothing about the old behaviour changes for them.
- The choice is re-made on every reconnect, so moving between home Wi-Fi
  and a tailnet-only network is handled without touching config. The
  heartbeat loop additionally calls `recheck_preferred()` so the client
  drifts *back* to the LAN address when it comes home.
- Bearer token from `config.server.token` on every request.
- Optional CA bundle (`config.server.ca_cert`) for self-signed certs.
- Connection state mirrors the old gRPC client (`is_connected`,
  `mark_connected`, `mark_disconnected`, `reconnect`) so the daemon's
  loops keep their existing shape.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from lios_sync.config import ClientConfig
from lios_sync.endpoints import Endpoint, candidates, normalise_url, probe, resolve

logger = logging.getLogger(__name__)

# Backoff schedule for reconnection attempts (seconds)
_BACKOFF_SCHEDULE = [5, 10, 30, 60]

# Per-request timeout — tool calls can be slow, push paths should be quick.
_TOOL_TIMEOUT = httpx.Timeout(connect=10.0, read=70.0, write=10.0, pool=10.0)
_PUSH_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


class AuthError(RuntimeError):
    """The server rejected our bearer token (401).

    Distinct from a generic connection failure so callers (the daemon's
    heartbeat loop, `lios-sync status`) can surface something a non-technical
    user can act on instead of a raw traceback.
    """

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "comar token rejected by the server — it may have expired. "
            "Ask Alex for a new install code, then re-run the installer."
        )


def _check_response(resp: httpx.Response) -> None:
    """Raise AuthError on 401, otherwise the usual httpx error on other failures."""
    if resp.status_code == 401:
        raise AuthError()
    resp.raise_for_status()


class ServerClient:
    """HTTP client for comar-server (`/api/v1/*`)."""

    def __init__(self, config: ClientConfig):
        self.config = config
        self._client: httpx.Client | None = None
        self._connected = False
        self._reconnect_attempt = 0
        self._lock = threading.Lock()
        # Which candidate URL we are currently dialling, and how it was reached
        # (lan / tailscale / …). None until the first _build_client().
        self.endpoint: Endpoint | None = None
        # The live SSE client, if a stream is open. Held so an endpoint switch
        # can tear it down (see `close_event_stream`).
        self._stream_client: httpx.Client | None = None
        self._build_client()

    # -- Lifecycle ------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """The URL currently in use — not necessarily `config.server.urls[0]`."""
        return self.endpoint.url if self.endpoint else normalise_url(self.config.server.url)

    def _build_client(self) -> None:
        """Construct the underlying httpx.Client with auth + TLS settings.

        Endpoint selection happens here rather than at config-load time so that
        every rebuild (startup, reconnect, coming back onto home Wi-Fi) re-runs
        it. `resolve()` returns None when nothing answers; we still build a
        client against the first candidate so the daemon has something concrete
        to retry and to report in its logs.
        """
        chosen = resolve(
            self.config.server.urls,
            self.config.server.token,
            self.config.server.ca_cert,
        )
        if chosen is None:
            fallback = candidates(self.config.server.urls)
            chosen = fallback[0] if fallback else Endpoint(url="http://localhost:9443", kind="loopback")
            logger.warning("No endpoint answered; will keep retrying %s", chosen.url)
        if self.endpoint is not None and self.endpoint.url != chosen.url:
            # The SSE stream captured the *old* base URL when it opened and
            # will happily sit there for hours, since sse-starlette's pings keep
            # the socket healthy. Drop it so the daemon's events loop reopens
            # against the endpoint we just chose.
            self.close_event_stream()
        self.endpoint = chosen
        base_url = chosen.url

        verify: Any = True
        ca_path = Path(self.config.server.ca_cert) if self.config.server.ca_cert else None
        if ca_path and ca_path.is_file():
            verify = str(ca_path)

        headers = {
            "Authorization": f"Bearer {self.config.server.token}",
            "User-Agent": f"comar-client/{_get_version()}",
        }

        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            verify=verify,
            timeout=_PUSH_TIMEOUT,
        )
        logger.info("HTTP client → %s [%s] (verify=%s)", base_url, chosen.kind, verify)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def mark_connected(self) -> None:
        self._connected = True
        self._reconnect_attempt = 0

    def mark_disconnected(self) -> None:
        self._connected = False

    def close(self) -> None:
        self.close_event_stream()
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False

    def reconnect(self) -> bool:
        """Backoff, re-resolve the endpoint, rebuild, and probe with a heartbeat.

        The re-resolve is the important part: a dropped connection is usually a
        *network change* (lid closed at home, opened in the office), so the URL
        that failed is frequently not the URL that should be tried next.
        """
        backoff = _BACKOFF_SCHEDULE[min(self._reconnect_attempt, len(_BACKOFF_SCHEDULE) - 1)]
        logger.info("Reconnecting to server (attempt %d, backoff %ds)", self._reconnect_attempt + 1, backoff)
        time.sleep(backoff)
        previous = self.endpoint
        with self._lock:
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
            self._build_client()
        if previous and self.endpoint and previous.url != self.endpoint.url:
            logger.info("Endpoint switched %s → %s", previous, self.endpoint)
        try:
            self.heartbeat()
            self.mark_connected()
            logger.info("Reconnected to server successfully")
            return True
        except Exception as e:  # noqa: BLE001
            self.mark_disconnected()
            self._reconnect_attempt += 1
            logger.warning("Reconnection failed: %s", e)
            return False

    def recheck_preferred(self) -> bool:
        """If we're on a fallback endpoint, switch back when the preferred one returns.

        Without this the client is sticky in one direction only: it correctly
        fails over LAN → Tailscale when leaving the house, then stays on
        Tailscale forever, routing every vault push and voice-memo upload out
        over WireGuard and back in again while sitting ten feet from the server.

        Called from the daemon's 5-minute heartbeat loop. Costs one short probe,
        and only when currently on a non-preferred endpoint — at home this is a
        no-op that never touches the network.

        Returns True if the endpoint changed.
        """
        cands = candidates(self.config.server.urls)
        if len(cands) < 2 or self.endpoint is None:
            return False
        preferred = cands[0]
        if self.endpoint.url == preferred.url:
            return False
        if not probe(preferred.url, self.config.server.token, self.config.server.ca_cert):
            return False

        logger.info("Preferred endpoint %s is back — switching from %s", preferred, self.endpoint)
        with self._lock:
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
            self._build_client()
        return True

    # -- Convenience: heartbeat ----------------------------------------------

    def heartbeat(self, task_health: dict | None = None) -> dict:
        """Single-poll liveness + sync hashes.

        task_health is the supervisor's per-task snapshot — the server
        stores it on this device's token row so a stalled loop is visible
        remotely (ISS-001 made split-brain invisible for days).

        Returns dict with `latest_client_version`, `latest_client_checksum`,
        `prompt_set_hash`, `server_time`. Verifies credentials.
        """
        params = {"client_version": _get_version()}
        if task_health:
            import json
            params["task_health"] = json.dumps(task_health)
        resp = self._client.get(
            "/api/v1/heartbeat",
            params=params,
            timeout=_PUSH_TIMEOUT,
        )
        _check_response(resp)
        return resp.json()

    # -- Prompts --------------------------------------------------------------

    def list_prompts(self) -> list[dict]:
        resp = self._client.get("/api/v1/prompts", timeout=_PUSH_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    # -- Instructions ---------------------------------------------------------

    def get_instructions(self) -> str:
        """Fetch the canonical MCP instructions preamble. Empty string on failure."""
        try:
            resp = self._client.get("/api/v1/instructions", timeout=_PUSH_TIMEOUT)
            resp.raise_for_status()
            return resp.json().get("instructions", "")
        except Exception:
            return ""

    # -- Commands (sam-live 0.3) ----------------------------------------------

    def get_commands(self) -> dict:
        """Fetch the day-to-day CLAUDE.md + slash commands the server serves.

        Returns `{"claude_md": str, "commands": {"<filename>.md": str, ...}}`.
        Same bearer auth as heartbeat, so a stale/revoked token surfaces here
        as AuthError too.
        """
        resp = self._client.get("/api/v1/commands", timeout=_PUSH_TIMEOUT)
        _check_response(resp)
        return resp.json()

    # -- Tools ----------------------------------------------------------------

    def list_tools(self) -> list[dict]:
        resp = self._client.get("/api/v1/tools", timeout=_PUSH_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a server tool. Returns the result as a JSON-encoded string.

        Raises RuntimeError on tool error.
        """
        resp = self._client.post(
            f"/api/v1/tools/{tool_name}",
            json=arguments or {},
            timeout=_TOOL_TIMEOUT,
        )
        if resp.status_code == 404:
            raise RuntimeError(f"Unknown tool: {tool_name}")
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"CallTool {tool_name} failed: {body.get('error', 'unknown')}")
        # Re-encode `result` as a JSON string so the MCP shim can pass it
        # through as a TextContent without an extra round-trip.
        result = body.get("result")
        if isinstance(result, str):
            return result
        return json.dumps(result)

    # -- Pushes ---------------------------------------------------------------

    def push_vault_file(self, path: str, content: str, file_hash: str) -> dict:
        resp = self._client.post(
            "/api/v1/vault/push",
            json={"path": path, "content": content, "file_hash": file_hash},
        )
        resp.raise_for_status()
        return resp.json()

    def ingest_inbox_file(
        self,
        filename: str,
        data: bytes,
        *,
        file_type: str | None = None,
        metadata: dict | None = None,
        timeout: float = 120.0,
    ) -> dict:
        """Upload a captured file to the server's inbox for processing.

        Base64 in a JSON body rather than multipart because that's the contract
        `/api/inbox/ingest` already serves for the Tines webhook — reusing it
        means no new endpoint and no new auth path.

        The upload is all the Mac does. Classification, duration, transcription
        and routing are the server's job; the daemon deliberately holds no
        OpenAI key and parses none of the container (see `voice_memos.py`).

        A longer timeout than the other pushes: these are megabytes over
        Wi-Fi/Tailscale, not a few KB of JSON.
        """
        import base64

        body: dict = {
            "filename": filename,
            "data": base64.b64encode(data).decode("ascii"),
        }
        if file_type:
            body["type"] = file_type
        if metadata:
            body["metadata"] = metadata

        resp = self._client.post("/api/inbox/ingest", json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def ack_reminder_command(
        self,
        command_id: int,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> dict:
        """Acknowledge an SSE-dispatched EventKit command back to the server.

        D.5: server queues reminders_add/reminders_complete commands and pushes
        them to the connected daemon over SSE. After execution, the daemon
        calls this to resolve the waiting Future on the server.
        """
        body: dict = {}
        if result is not None:
            body["result"] = result
        if error is not None:
            body["error"] = error
        resp = self._client.post(
            f"/api/v1/reminders/commands/{command_id}/done",
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    def push_reminders(self, reminders: list[dict]) -> dict:
        # `reminders` items already match the server schema (uid/list_name/etc).
        # `account_email` is forwarded if present; ignored server-side until
        # multi-user attribution lands.
        resp = self._client.post(
            "/api/v1/reminders/push",
            json={"reminders": reminders},
        )
        resp.raise_for_status()
        return resp.json()

    def report_reminders_verified(self) -> dict:
        """Tell the server the reminders bridge just polled EventKit and is alive.

        Called every iteration of the daemon's reminders loop — whether
        push_reminders fired or not. Server stamps
        `users.reminders_verified_at = now()` so `data_freshness` can tell
        a quiet bridge from a dead one.
        """
        resp = self._client.post("/api/v1/reminders/verified", json={})
        resp.raise_for_status()
        return resp.json()

    def push_health(
        self,
        daily_metrics: list[dict],
        workouts: list[dict],
        sleep_sessions: list[dict],
    ) -> dict:
        resp = self._client.post(
            "/api/v1/health/push",
            json={
                "daily_metrics": daily_metrics,
                "workouts": workouts,
                "sleep_sessions": sleep_sessions,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def push_logs(self, entries: list[dict]) -> dict:
        """Ship buffered log entries. `timestamp` may be a datetime or epoch int."""
        serialised = []
        for e in entries:
            ts = e.get("timestamp")
            if isinstance(ts, datetime):
                ts_str = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()
            elif isinstance(ts, (int, float)):
                ts_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            else:
                ts_str = str(ts) if ts else datetime.now(timezone.utc).isoformat()
            serialised.append({
                "timestamp": ts_str,
                "level": e["level"],
                "logger": e["logger"],
                "message": e["message"][:5000],
            })
        resp = self._client.post(
            "/api/v1/logs/push",
            json={"client_version": _get_version(), "entries": serialised},
        )
        resp.raise_for_status()
        return resp.json()

    # -- SSE event stream -----------------------------------------------------

    def open_event_stream(self):
        """Yield SSE events as dicts. Caller owns the loop; reconnect externally.

        Yields:
            dict with keys at least `type` (str), plus event-specific payload.
        """
        # Build a fresh client with no read timeout for the long-lived stream.
        base_url = str(self._client.base_url)
        headers = dict(self._client.headers)
        verify: Any = True
        ca_path = Path(self.config.server.ca_cert) if self.config.server.ca_cert else None
        if ca_path and ca_path.is_file():
            verify = str(ca_path)

        with httpx.Client(
            base_url=base_url, headers=headers, verify=verify,
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
        ) as stream_client:
            self._stream_client = stream_client
            try:
                with stream_client.stream("GET", "/api/v1/events") as resp:
                    _check_response(resp)
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            logger.debug("SSE: skipping non-JSON payload: %s", payload[:80])
            finally:
                self._stream_client = None

    def close_event_stream(self) -> None:
        """Tear down the live SSE stream, if any, so the caller reopens it.

        Safe to call from another thread: `iter_lines` raises out of the closed
        transport and the daemon's events loop treats that like any other stream
        error — backoff, then reconnect against the current base URL.
        """
        stream_client = self._stream_client
        if stream_client is None:
            return
        try:
            stream_client.close()
        except Exception:  # noqa: BLE001
            pass


def _get_version() -> str:
    try:
        from lios_sync import __version__
        return __version__
    except Exception:
        return "unknown"
