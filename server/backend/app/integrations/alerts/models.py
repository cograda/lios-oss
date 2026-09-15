"""alert_events — the reviewable log of monitoring alerts (lios#230).

`notifications`'s HA-push sink is deliberately noisy-averse (persistence
gate, re-fire cooldown, quiet hours) but has no notion of *severity at all*
— everything that reaches `system_alerts` eventually pushes. Alertmanager
sits entirely outside that system (see `deploy/monitoring/`) and pushes
straight to Pushover with a flat priority, which is the "random log stuff"
issue #230 complains about.

This table is the other half of the fix: a durable ledger of every alert
Alertmanager's webhook receiver fires or resolves, `?page == "phone"`-tagged
or not, so a kickoff/check-in can say "these fired/cleared since yesterday"
instead of everything buzzing the phone. Household-shared (no
`UserOwnedMixin`) — tech-health, like the snag register: an alert belongs
to the house, not to whichever user happens to be looking at the dashboard.

One row per `alerts[]` entry in a webhook delivery (never one row per
delivery) — Alertmanager's own payload already flattens a group into
one-or-more independent alerts, and each has its own fingerprint/labels/
annotations, so collapsing them back into one row would lose exactly the
per-alert detail a reviewable log needs.

Idempotency: Alertmanager resends the same firing alert every
`repeat_interval` with an identical `(fingerprint, status, startsAt)`
triple, so those three columns carry a unique index and the inlet
(`routes.py`) upserts with `ON CONFLICT DO NOTHING` — a resend is silently
absorbed, never a duplicate row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base

STATUSES = ("firing", "resolved")


class AlertEvent(Base):
    """One `alerts[]` entry from one Alertmanager webhook delivery."""

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    # Alertmanager's own stable identifier for this alert (a hash of its
    # labels) — the anchor idempotency and "is this still firing" both key
    # off, not the rendered text (which carries an age that changes every
    # delivery — the same rule `notifications/sweep.py`'s fingerprinting
    # already follows).
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    alertname: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # From labels — nullable because not every alert rule sets one.
    severity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # From labels. "phone" marks the ones that were meant to buzz a
    # handset (mirrors Alertmanager's own routing label in
    # `deploy/monitoring/alertmanager/alertmanager.yml`) — everything else
    # is FYI, reviewed at the next kickoff/check-in rather than pushed.
    page: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # `instance` (Prometheus target scrape label) or `host` (Grafana/Loki
    # log-alert label) — whichever the rule carries. Nullable: some rules
    # (the heartbeat canary) carry neither.
    instance: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL while firing (or when Alertmanager's own zero-value timestamp
    # arrives instead of a real one, e.g. "0001-01-01T00:00:00Z") — see
    # `routes.py::_parse_ends_at`. Set once a `status="resolved"` delivery
    # carries a real value.
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Full label/annotation sets, verbatim — every accepted delivery is
    # stored whatever its shape, same principle as `signals.SignalEvent`,
    # so a rule this table doesn't yet have named columns for still
    # preserves its detail rather than silently dropping it.
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    annotations: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_alert_events_received_at", "received_at"),
        Index("ix_alert_events_fingerprint_status", "fingerprint", "status"),
        Index(
            "uq_alert_events_fingerprint_status_starts_at",
            "fingerprint", "status", "starts_at",
            unique=True,
        ),
    )
