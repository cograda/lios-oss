"""Best-effort persistence for the tool_calls audit trail.

Written from `app.plugin.dispatch.dispatch_tool` — the one chokepoint both
transports (`app/mcp/server.py::call_tool` and `app/api/v1.py::call_tool`)
route through — after every call (ok, error, or timeout).

`record_tool_call` opens its own short-lived session via `get_db()`,
independent of the tool handler's own session/transaction, and NEVER lets a
persistence failure break tool dispatch: any exception here is caught and
logged as a warning, not raised. Callers should run this off the event loop
(`asyncio.to_thread`) since it does a blocking DB round-trip.

V4 chunk 2.5: also persists `args_summary` (redacted — see
`app.services.redaction.scrub_args`), `affected` (JSON-encoded list of
entity refs, or None), `source_ip`, and `transport`.
"""

import json
import logging
from datetime import datetime, timezone

from app.db import get_db
from app.models.tool_calls import ToolCall

logger = logging.getLogger(__name__)

# Errors can be arbitrarily long (stack-trace-derived messages); cap what we
# store so one verbose exception doesn't bloat the audit table.
_MAX_ERROR_LEN = 2000


def record_tool_call(
    *,
    name: str,
    user_id: int | None,
    duration_ms: int,
    status: str,
    error: str | None,
    tool_call_id: str,
    args_summary: str | None = None,
    affected: list[str] | None = None,
    source_ip: str | None = None,
    transport: str | None = None,
) -> None:
    """Insert one `tool_calls` row. Never raises — logs a warning on failure."""
    try:
        db = get_db()
        with db.session() as session:
            session.add(ToolCall(
                tool_call_id=tool_call_id,
                name=name,
                user_id=user_id,
                duration_ms=duration_ms,
                status=status,
                error=error[:_MAX_ERROR_LEN] if error else None,
                called_at=datetime.now(timezone.utc),
                args_summary=args_summary,
                affected=json.dumps(affected) if affected else None,
                source_ip=source_ip,
                # The column is 10 chars ("mcp"|"http"). A longer label from a
                # script or a new transport must shorten, not lose the row —
                # on 2026-09-02 a 21-char label dropped the audit for ~60
                # dispatches while the calls themselves succeeded.
                transport=transport[:10] if transport else None,
            ))
            session.commit()
    except Exception:
        logger.warning(
            "Failed to record tool_calls row for %s (tool_call_id=%s)",
            name, tool_call_id, exc_info=True,
        )
