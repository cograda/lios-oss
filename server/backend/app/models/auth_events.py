"""auth_events — audit trail for authentication outcomes (V4 chunk 2.5).

One row per 401 (either transport) and per explicit token lifecycle event
(install-code redemption = "issued", `DELETE /api/auth/clients/{id}` =
"revoked"). Written best-effort by `app.services.auth_events.record_auth_event`
— never allowed to break the auth path it's observing.

Not `UserOwnedMixin` — `user_id` is nullable (a failed-auth attempt usually
has no resolved user at all) and this is an admin/ops audit log, not
per-user application data. `ON DELETE SET NULL` so removing a user doesn't
cascade into the audit trail. Pruned daily alongside `tool_calls` /
`client_logs` (see `app/plugin/kernel_jobs.py::run_prune_auth_events`).
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class AuthEvent(Base):
    __tablename__ = "auth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    # "401" | "issued" | "revoked" — kept as a free string rather than an
    # enum so a new outcome doesn't need a migration.
    outcome: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    token_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transport: Mapped[str | None] = mapped_column(String(10), nullable=True)  # mcp|http
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
