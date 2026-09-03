"""Status display — shows daemon, server, MCP, and sync state."""

import json
import logging
import urllib.request

from lios_sync.config import load_config, CONFIG_FILE
from lios_sync.launchd import is_installed, is_running, get_pid

logger = logging.getLogger(__name__)


def show_status():
    """Print current daemon/server/MCP status."""
    config = load_config()

    if not CONFIG_FILE.exists():
        print("  Not configured. Run `lios-sync setup` first.")
        return

    print(f"\n  lios-sync status (user: {config.user})\n")

    # Daemon
    if is_installed():
        if is_running():
            pid = get_pid()
            pid_str = f" (pid {pid})" if pid else ""
            _ok(f"Daemon:      running{pid_str}")
        else:
            _warn("Daemon:      installed but not running")
    else:
        _warn("Daemon:      not installed (run `lios-sync install`)")

    # Server (HTTP). ServerClient resolves the endpoint in its constructor, so
    # by the time heartbeat() returns, `client.endpoint` says which of the
    # configured URLs won and over what kind of path.
    try:
        from lios_sync.server_client import AuthError, ServerClient
        client = ServerClient(config)
        client.heartbeat()
        _ok(f"Server:      connected ({client.endpoint})")
        client.close()
    except AuthError as e:
        # Friendly message baked into AuthError itself — no traceback, no
        # generic "connection failed" for what is actually an expired token.
        _err(f"Server:      {e}")
    except Exception as e:
        _err(f"Server:      {e}")

    # Endpoint detail — only worth printing when there's an actual choice to
    # make. Probing every candidate (rather than stopping at the first hit) is
    # the point: "LAN is down and you silently fell back to Tailscale" is
    # exactly the state that's otherwise invisible.
    from lios_sync.endpoints import candidates, describe, tailscale_up

    if len(candidates(config.server.urls)) > 1:
        for endpoint, reachable in describe(
            config.server.urls, config.server.token, config.server.ca_cert
        ):
            (_ok if reachable else _warn)(
                f"  {endpoint.kind:<10} {endpoint.url}"
                f"{'' if reachable else '  (no answer)'}"
            )
        if not tailscale_up():
            _warn("  tailscale  not connected on this machine")

    # Local health endpoint (side-car — no longer an MCP server, see ISS-002)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{config.health_port}/health")
        resp = urllib.request.urlopen(req, timeout=2)
        data = json.loads(resp.read())
        tasks = data.get("tasks", {}) or {}
        # A task is "alive" if it hasn't finished and either never beats by
        # design (health uvicorn task, beats=False → alive_seconds_ago None)
        # or has beaten within 2× the slowest beat cycle (heartbeat/events
        # beat every ~300s; a 60s threshold miscounted them as dead).
        alive = sum(
            1 for t in tasks.values()
            if t.get("finished") is False
            and (t.get("alive_seconds_ago") is None or t.get("alive_seconds_ago") < 660)
        )
        total = len(tasks)
        _ok(f"Client:      healthy ({alive}/{total} tasks alive)")
    except Exception:
        _warn(f"Client:      not responding on localhost:{config.health_port}")

    # Vault
    from pathlib import Path
    vault_path = Path(config.vault.path)
    if vault_path.is_dir():
        md_count = len(list(vault_path.rglob("*.md")))
        _ok(f"Vault:       watching ({md_count} .md files)")
    else:
        _warn(f"Vault:       path not found ({vault_path})")

    print()


def _ok(msg: str):
    print(f"  ✓ {msg}")

def _warn(msg: str):
    print(f"  ⚠ {msg}")

def _err(msg: str):
    print(f"  ✗ {msg}")
