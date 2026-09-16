"""Best-effort persistence for the auth_events audit trail (V4 chunk 2.5).

Mirrors `app.services.runs.record_tool_call`: opens its own short-lived
session via `get_db()`, independent of whatever's calling it, and NEVER lets
a persistence failure break the auth path it's observing — an audit-logging
bug must not turn into a denial-of-service or a broken login.

Called from:
  - `app.auth.client_token.get_current_user` (HTTP 401s)
  - `app.mcp.server._authenticate_request` (MCP 401s)
  - `app.routes.install.fetch_install_script` (token "issued" on redemption)
  - `app.routes.auth.deactivate_client` (token "revoked")
"""

import logging
from datetime import datetime, timezone

from app.db import get_db
from app.models.auth_events import AuthEvent

logger = logging.getLogger(__name__)


def record_auth_event(
    *,
    outcome: str,
    token_last4: str | None = None,
    source_ip: str | None = None,
    transport: str | None = None,
    user_id: int | None = None,
) -> None:
    """Insert one `auth_events` row. Never raises — logs a warning on failure."""
    # F8: a 401 is also what feeds the per-IP rate limiter. Doing it here —
    # the one chokepoint every bearer-path failure already funnels through —
    # means no current or future failure path can forget to count itself.
    # (The DB insert below stays best-effort; the budget spend must not.)
    if outcome == "401" and source_ip:
        from app.auth.rate_limit import record_failure

        record_failure(source_ip)
    try:
        db = get_db()
        with db.session() as session:
            session.add(AuthEvent(
                outcome=outcome,
                token_last4=token_last4 or None,
                source_ip=source_ip,
                transport=transport,
                user_id=user_id,
                ts=datetime.now(timezone.utc),
            ))
            session.commit()
    except Exception:
        logger.warning(
            "Failed to record auth_events row (outcome=%s, transport=%s)",
            outcome, transport, exc_info=True,
        )
