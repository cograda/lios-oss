"""Local health endpoint — daemon side-car liveness check.

Post Phase 4 (2026-07-14) the daemon is no longer an MCP server: Claude
Code and every other consumer talk Streamable HTTP directly to the comar
server's /mcp/. All that remains locally is a tiny health check on the old
mcp_port so `lios-sync status` (and anyone else on the LAN) can see the daemon
is alive and inspect per-task liveness from the TaskSupervisor.

Dependency-light on purpose: Starlette + uvicorn only.
"""

import logging
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lios_sync import __version__

if TYPE_CHECKING:
    from lios_sync.vault_watcher import VaultHandler
    from lios_sync.voice_memos import VoiceMemoHandler

logger = logging.getLogger(__name__)


def create_health_app(
    vault_handler: "VaultHandler | None" = None,
    supervisor=None,
    voice_memo_handler: "VoiceMemoHandler | None" = None,
    voice_memo_state: str = "disabled",
    voice_memo_detail: str = "",
) -> Starlette:
    """Create the minimal health-check ASGI app.

    Returns a Starlette app with a single GET /health route reporting
    daemon version, per-task liveness (from the TaskSupervisor), and
    watcher state.

    ⚠️ **`voice_memo_watcher_active` used to be deliberately ambiguous** — one
    boolean covering both "disabled in config" and "enabled but Full Disk Access
    was refused", on the reasoning that the log names the fix and a health
    endpoint should not report on a permissions dialog.

    That was wrong, and it cost 19 days of silent non-capture: config said
    `enabled = true`, /health said `voice_memo_watcher_active: false`, and the
    two together were the entire bug — trivially checkable, and checked by
    nobody, because `false` was also what "switched off" looked like. 110
    eligible recordings, 0 uploads, no ledger file ever written.

    Distinguishing "off" from "broken" is precisely a health endpoint's job.
    `voice_memo_state` is therefore tri-state — `disabled` | `failed` | `active`
    — with `voice_memo_detail` carrying the actionable text. The boolean stays
    for backwards compatibility with anything already reading it.
    """

    async def handle_health(request: Request):
        return JSONResponse({
            "status": "ok",
            "version": __version__,
            "vault_watcher_active": vault_handler is not None,
            "retry_queue_depth": vault_handler.retry_queue_depth if vault_handler else 0,
            # Retained for compatibility; prefer `voice_memo_state`, which can
            # tell "off" apart from "broken".
            "voice_memo_watcher_active": voice_memo_handler is not None,
            "voice_memo_state": voice_memo_state,
            "voice_memo_detail": voice_memo_detail,
            "voice_memos_uploaded": (
                voice_memo_handler.uploaded if voice_memo_handler else 0
            ),
            "voice_memos_failed": (
                voice_memo_handler.failed if voice_memo_handler else 0
            ),
            "tasks": supervisor.health() if supervisor else {},
        })

    return Starlette(routes=[Route("/health", endpoint=handle_health)])
