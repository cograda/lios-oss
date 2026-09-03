"""NotificationSend — the send ledger that makes alerting bearable.

This table is the reason this integration owns any storage at all. A sweep
that just published whatever `system_alerts` returned would push the same
"obsidian data stale" message every 15 minutes, forever, until the phone got
muted — at which point the alerting system is worse than none, because now the
one channel that would have carried a real outage is ignored.

So every distinct problem gets a **fingerprint** and one open row. See
`sweep.py::_fingerprint` for how the fingerprint is derived; the critical part
is that it keys on the *kind* of issue, never on the rendered text, because the
rendered text contains an age ("last sync 3h 12m ago") that changes on every
single sweep and would defeat deduplication completely.

Lifecycle of a row:

    first seen  →  INSERT (resolved_at NULL), publish
    still open  →  publish again only once `resend_after_minutes` has passed
    gone        →  set resolved_at, optionally publish a recovery message
    seen again  →  a NEW row (the old one stays as history)

Household-shared — no `UserOwnedMixin`. These are infrastructure alerts about
the deployment, not personal data, and they go to a single configured topic.
Per-user notification routing would need a per-user topic/token first; there is
no user to attribute a stale-sync alert to.

**F7 exception**: some alerts ARE user-attributable (the health data-coverage
gap issue names a specific user in its body). `user_id` below is a plain
nullable FK — NOT `UserOwnedMixin`, which is NOT NULL — because NULL (no
single owner, household-shared) is the normal case here, not an edge case.
`notify_recent` scopes reads on it: NULL rows plus the caller's own.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class NotificationSend(Base):
    __tablename__ = "notification_sends"

    id: Mapped[int] = mapped_column(primary_key=True)

    # NULL = household-shared (no single owner — most rows). Set only when the
    # sweep can attribute the underlying issue to one user (see F7 note above).
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Stable identity for one distinct problem, e.g.
    # "integration:obsidian:data_stale" or "reauth:google:someone@example.com".
    # Deliberately NOT unique: a problem that recurs after being resolved gets
    # a second row so the history shows both episodes. Uniqueness that matters
    # ("only one OPEN row per fingerprint") is enforced by the partial index
    # below, which the DB can express and a plain unique constraint cannot.
    fingerprint: Mapped[str] = mapped_column(String(255), index=True)

    # What was actually published, kept verbatim for the audit trail. `body`
    # carries the age-bearing text that `fingerprint` deliberately excludes.
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    # "warning" | "critical" | "recovery" — maps to a Home Assistant mobile-app
    # push payload (interruption-level/priority/ttl) in client.py::_SEVERITY_DATA.
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    topic: Mapped[str] = mapped_column(String(100))

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    # NULL when the alert was recorded but publishing failed — the row still
    # suppresses duplicates, and `send_count` staying 0 shows it never landed.
    last_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    send_count: Mapped[int] = mapped_column(Integer, default=0)

    # NULL means "still broken". Set when a sweep no longer sees the
    # fingerprint, which is also what closes the row out for dedup purposes.
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    # NULL means "not currently held" — the normal case, and true for every
    # row that has actually reached a phone. Set by `sweep.py`'s push gate
    # ("min_active_gate" | "refire_cooldown" | "quiet_hours") whenever a
    # sweep decides the alert is real but not yet *deliverable*, and cleared
    # the moment a push actually lands. Exists so a hold is queryable via
    # `notify_recent` rather than looking, from the outside, identical to a
    # sweep that silently forgot about the row — see notifications/sweep.py's
    # module docstring for why suppression sits at this exact boundary.
    suppressed_reason: Mapped[str | None] = mapped_column(
        String(40), nullable=True,
    )

    __table_args__ = (
        # At most one OPEN alert per fingerprint. Partial (WHERE resolved_at IS
        # NULL) so resolved history is unconstrained and can accumulate freely.
        Index(
            "ix_notification_sends_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=(resolved_at.is_(None)),
        ),
    )
