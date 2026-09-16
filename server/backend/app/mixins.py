"""Reusable SQLAlchemy mixins for integration models."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column


class UserOwnedMixin:
    """Standard multi-tenancy column for per-user data.

    Every per-user table must include this mixin. Cross-user / shared tables
    (finance, historical_corpus, weather, calendar, home assistant, tasks,
    snags, irish_rail — see `app/privacy.py::HOUSEHOLD_SHARED_TABLES` for the
    full, reasoned list) deliberately do NOT include it — they are
    household-scoped or fully shared. NOTE: this docstring previously listed
    "vault" here too, which was wrong — `VaultChunk` (`app/integrations/
    obsidian/models.py`) IS `UserOwnedMixin`; each user's vault is entirely
    their own (see the root instructions block).

    Composite uniqueness on user-scoped tables MUST start with user_id, e.g.
    UniqueConstraint("user_id", "uid"). Bare `uid` constraints will collide
    across users.

    Cascade is RESTRICT: deleting a user with data fails loudly; cleanup must
    be explicit.
    """

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class SourcedRecordMixin:
    """Standardizes infrastructure columns across integration-sourced records.

    Provides four columns that every record pulled from an external system
    should have, enabling generic dedup, change detection, and cross-source
    queries.

    Columns:
        source_id    — the external system's unique identifier for this record
        source_ts    — when the event happened in the source system
        synced_at    — when we last synced/updated this record
        content_hash — MD5 of content, for dedup and change detection
    """

    source_id: Mapped[str | None] = mapped_column(
        String(500), nullable=True, index=True,
    )
    source_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
