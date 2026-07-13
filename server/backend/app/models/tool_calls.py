"""Per-call audit trail for MCP/HTTP tool dispatch.

One row per tool invocation (both the MCP `call_tool` path in
`app/mcp/server.py` and the HTTP `/api/v1/tools/{name}` path in
`app/api/v1.py`), written by `app.services.tool_calls.record_tool_call`.
Feeds `system_alerts` (repeated-failure + p95-latency checks) and the
dashboard.

Not `UserOwnedMixin` — `user_id` is nullable (a call can, in principle,
run without a resolved user — defensive, not expected in practice) and
this table is an admin/ops audit log, not per-user application data.
`ON DELETE SET NULL` so a user row can be removed without cascading into
the audit trail.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Short per-call correlation id (secrets.token_hex(4)), also emitted in
    # the structured completion log line — lets an ops person grep a log
    # line and find the matching DB row (or vice versa).
    tool_call_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # ok|error|timeout
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
