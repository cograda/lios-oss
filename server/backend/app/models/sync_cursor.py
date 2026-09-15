"""Generic per-(integration, user, key) sync cursor bookkeeping.

V4 chunk 3.2. Replaces ad-hoc cursor storage (e.g. lastfm's backfill page
stashed as a string in `SyncState.last_error`, see
`app/integrations/lastfm/sync.py::_get_backfill_state`) with one small,
opt-in table any integration can use via `app.plugin.sync_runtime.SyncCursor`.
Nothing is migrated onto this automatically in this chunk — existing
bespoke cursors keep working; new/rewritten sync logic can adopt this
instead of inventing another one-off column.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class SyncCursorRow(Base):
    """One row per (integration, user, key) cursor value.

    `user_id` is nullable — some integrations (weather, lastfm today) sync
    a single global account, not per-user, so the cursor has no owning user.
    Per-user integrations should always pass a `user_id`.
    """

    __tablename__ = "sync_cursors"
    __table_args__ = (
        UniqueConstraint(
            "integration", "user_id", "key",
            name="uq_sync_cursors_integration_user_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    integration: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
