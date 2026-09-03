"""User model — the identity table that anchors all per-user data.

Seeded with two rows by the multi-user migration:
    (1, 'alex',  'Alex')
    (2, 'sam', 'Sam')

`name` is the canonical short identifier (lowercase, used in code paths
like `Daily Notes/Alex/`). `display_name` is for UI rendering.

Add fields here (timezone, default_calendar_account, avatar_url, etc.) as
new surfaces need them — every per-user table FKs to this table, so adding
fields is cheap and localised.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Bridge-liveness signal for the comar-client reminders loop. Distinct
    # from Reminder.synced_at, which is data-change-time. The daemon bumps
    # this on every poll iteration (whether or not data changed) so
    # data_freshness for apple_reminders can distinguish "nothing changed"
    # from "daemon dead".
    reminders_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Tone for server-rendered prose (MCP instructions, morning briefing
    # framing where such text exists): "direct" (default, Alex — concise,
    # direct, current behavior) or "curious" (Sam — warm/curious, patterns
    # surfaced as questions, no guilt-inducing framing around missed
    # tasks/streaks). See sam-rollout Plan D2.
    voice_profile: Mapped[str] = mapped_column(
        String(20), default="direct", server_default="direct"
    )

    def __repr__(self) -> str:
        return f"<User {self.name}>"
