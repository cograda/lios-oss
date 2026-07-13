"""Status display — shows daemon, server, MCP, and sync state."""

import json
import logging
import urllib.request

from comar.config import load_config, CONFIG_FILE
from comar.launchd import is_installed, is_running, get_pid

logger = logging.getLogger(__name__)


def show_status():
    """Print current daemon/server/MCP status."""
    config = load_config()

    if not CONFIG_FILE.exists():
        print("  Not configured. Run `comar setup` first.")
        return

    print(f"\n  comar status (user: {config.user})\n")

    # Daemon
    if is_installed():
        if is_running():
            pid = get_pid()
            pid_str = f" (pid {pid})" if pid else ""
            _ok(f"Daemon:      running{pid_str}")
        else:
            _warn("Daemon:      installed but not running")
    else:
        _warn("Daemon:      not installed (run `comar install`)")

    # Server (HTTP)
    try:
        from comar.server_client import ServerClient
        client = ServerClient(config)
        hb = client.heartbeat()
        tool_count = hb.get("tool_count", "?") if isinstance(hb, dict) else "?"
        # ServerConfig.url returns urls[0] for backwards compat with multi-URL fallback
        _ok(f"Server:      connected ({config.server.url})")
        client.close()
    except Exception as e:
        _err(f"Server:      {e}")

    # Local MCP server
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{config.mcp_port}/health")
        resp = urllib.request.urlopen(req, timeout=2)
        data = json.loads(resp.read())
        tool_count = data.get("tools", "?")
        _ok(f"MCP:         listening on localhost:{config.mcp_port} ({tool_count} tools)")
    except Exception:
        _warn(f"MCP:         not responding on localhost:{config.mcp_port}")

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
