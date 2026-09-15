"""WhatsApp bridge health heartbeat — moved out of app/scheduler.py (V4 3.1).

Probes the bridge sidecar's HTTP health endpoint on a cron schedule declared
in this package's `manifest.py` (`background_tasks`, kind="cron"). The URL
comes from this integration's own config (`whatsapp_bridge_url`), not a
kernel literal — the kernel's scheduler never mentions "whatsapp" by name.
"""

import logging
import urllib.request

from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

# Default docker-network hostname for the bridge sidecar (docker-compose
# service name), used when HOME_WHATSAPP_BRIDGE_URL isn't set. Lives here
# (not in app/config.py) so the kernel's settings module carries no
# integration-specific literal.
_DEFAULT_BRIDGE_URL = "http://whatsapp-bridge:3100"


def _bridge_targets() -> list[tuple[str, str]]:
    """`(sync_state key, base url)` for every bridge that should be probed.

    One Baileys session per phone number means multi-user WhatsApp is multiple
    containers, so liveness has to be per-bridge. Each gets its own SyncState
    key: a single shared row would go green whenever *any* bridge answered,
    which is precisely the case where one person's messages have silently
    stopped arriving.
    """
    cfg = plugin_config("whatsapp")
    targets = [("whatsapp_bridge", cfg.whatsapp_bridge_url or _DEFAULT_BRIDGE_URL)]
    for index, extra in enumerate(cfg.whatsapp_bridge_urls or [], start=2):
        extra = (extra or "").strip()
        if extra:
            targets.append((f"whatsapp_bridge_{index}", extra))
    return targets


def _probe_one(key: str, base: str) -> bool:
    from app.scheduler import _update_sync_state

    url = f"{base.rstrip('/')}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            ok = 200 <= resp.status < 300
            error = None if ok else f"HTTP {resp.status}"
    except Exception as e:
        ok = False
        error = str(e)[:200]

    _update_sync_state(
        key,
        status="ok" if ok else "error",
        error=error,
        trigger="heartbeat",
    )
    return ok


def _check_bridge_blocking() -> bool:
    """Probe every configured bridge's health endpoint, writing a SyncState row
    each.

    The rows let the alerts layer distinguish "bridge dead" (no recent
    heartbeat) from "bridge alive but writes are failing" (the data-freshness
    probe says messages have stopped).

    Returns True only if *every* bridge answered healthy — this is the body
    behind `WhatsAppIntegration.probe()` (V4 chunk 4.3, batch B). Each bridge
    is probed regardless of an earlier one failing, so one dead container
    doesn't hide the state of the others.
    """
    results = [_probe_one(key, base) for key, base in _bridge_targets()]
    return all(results)


async def run_heartbeat() -> None:
    """Heartbeat the WhatsApp bridge container (cron: every minute)."""
    import asyncio

    try:
        await asyncio.to_thread(_check_bridge_blocking)
    except Exception:
        logger.exception("WhatsApp bridge heartbeat failed")
