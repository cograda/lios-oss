"""alert_events — the Alertmanager webhook inlet (lios#230)

Revision ID: a6f1c4b7d8e5
Revises: 403950e87a6f
Create Date: 2026-09-14

Household-shared, no `UserOwnedMixin` — tech-health, following `snags`/
`signals` (see `app/integrations/alerts/models.py` for the full design).

Dropped IF EXISTS before creation, per the repo's migration-safety
convention (see the 2026-09-07 tasks.kind/severity migration's docstring
for the crash-loop this guards against).

The unique index on (fingerprint, status, starts_at) is what makes the
inlet's repeat-delivery idempotency (`ON CONFLICT DO NOTHING`) work —
Alertmanager resends the same firing alert every `repeat_interval`, and a
resend carries the identical fingerprint/status/startsAt triple.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "a6f1c4b7d8e5"
down_revision = "403950e87a6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS alert_events")
    op.create_table(
        "alert_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("alertname", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("severity", sa.String(length=50), nullable=True),
        sa.Column("page", sa.String(length=50), nullable=True),
        sa.Column("instance", sa.String(length=200), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("labels", JSONB(), nullable=False, server_default="{}"),
        sa.Column("annotations", JSONB(), nullable=False, server_default="{}"),
    )
    op.execute("DROP INDEX IF EXISTS ix_alert_events_received_at")
    op.create_index("ix_alert_events_received_at", "alert_events", ["received_at"])
    op.execute("DROP INDEX IF EXISTS ix_alert_events_fingerprint_status")
    op.create_index(
        "ix_alert_events_fingerprint_status", "alert_events", ["fingerprint", "status"],
    )
    op.execute("DROP INDEX IF EXISTS uq_alert_events_fingerprint_status_starts_at")
    op.create_index(
        "uq_alert_events_fingerprint_status_starts_at",
        "alert_events", ["fingerprint", "status", "starts_at"],
        unique=True,
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_alert_events_fingerprint_status_starts_at")
    op.execute("DROP INDEX IF EXISTS ix_alert_events_fingerprint_status")
    op.execute("DROP INDEX IF EXISTS ix_alert_events_received_at")
    op.drop_table("alert_events")
