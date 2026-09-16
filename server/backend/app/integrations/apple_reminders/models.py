"""SQLAlchemy models for Apple Reminders integration."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin, UserOwnedMixin


class Reminder(UserOwnedMixin, SourcedRecordMixin, Base):
    __tablename__ = "reminders"
    __table_args__ = (
        # EventKit UIDs are scoped per iCloud account; user_id+uid is the
        # safe composite. account_email further disambiguates when one user
        # has multiple iCloud accounts (personal + work) on the same Mac.
        UniqueConstraint("user_id", "uid", name="uq_reminders_user_uid"),
        Index("ix_reminders_user_due", "user_id", "due_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Apple Reminders identifiers
    uid: Mapped[str] = mapped_column(String(255), index=True)
    account_email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    list_name: Mapped[str] = mapped_column(String(255), index=True)

    # Reminder content
    summary: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # 0=none, 1=high, 5=medium, 9=low
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Vault sync tracking
    vault_task_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sync_direction: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 'from_apple', 'from_server'

    # The ledger inlet (E chunk 6b, 2026-09-04): the `tasks.uid` this reminder
    # was captured into, or that it is now tracking. Set once, on capture
    # (`app.integrations.tasks.reminders_inlet::_capture_new`, via this
    # package's own facade), and never cleared — a reminder deleted on
    # the device does NOT drop the linked task (see the integration README's
    # deletion rule), so there is no event that should ever null this out.
    # A plain string column, not a foreign key: apple_reminders may not
    # import `tasks.models` (capability boundary), and `tasks.uid` — not a
    # numeric id — is this codebase's convention for a cross-package
    # reference (see `TaskLink.target_ref`).
    linked_task_uid: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    # Timestamps
    # synced_at inherited from SourcedRecordMixin
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReminderCommand(UserOwnedMixin, Base):
    """Queued commands for the Mac agent to execute."""

    __tablename__ = "reminder_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(50))  # 'add', 'complete'
    payload: Mapped[str] = mapped_column(Text)  # JSON
    # 'pending' — waiting to be dispatched (or re-dispatched).
    # 'draining' — claimed by a `drain_pending` call that hasn't finished
    #   dispatching it yet (`commands.py::_claim`). Transient: it either
    #   becomes 'superseded' (dispatch landed) or is released back to
    #   'pending' (dispatch failed, or the payload was unreadable). If the
    #   process dies mid-claim, `drain_pending` resets anything left in
    #   'draining' past `DRAINING_CLAIM_TIMEOUT` back to 'pending' before
    #   selecting candidates — see `claimed_at` below.
    # 'superseded' — replaced by a fresh row `dispatch_command` created
    #   while replaying this one; this row is done, the new one is live.
    # 'expired' — too old to replay (`MAX_REPLAY_AGE`); the write was lost.
    # 'done' / 'failed' — the device acked, one way or the other.
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When `_claim` last moved this row into 'draining'. NULL otherwise
    # (including once it leaves 'draining' either way) — only meaningful
    # while status == 'draining', for recovering a claim orphaned by a
    # process death between claim and dispatch.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
