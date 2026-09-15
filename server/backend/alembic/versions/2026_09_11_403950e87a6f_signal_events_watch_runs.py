"""signal_events + watch_runs — the signals inlet + watcher framework

Revision ID: 403950e87a6f
Revises: b3d7f4a2c8e1
Create Date: 2026-09-11

`app/integrations/signals/models.py` (2026-09-11 design note: `vault/
Projects/lios/Plans/2026-09-11 Signals — camera events, watchers and the
milk watcher.md`). Household-shared, no `UserOwnedMixin` — following
`tasks`/`snags`.

Both objects are dropped IF EXISTS before creation, per the repo's
migration-safety convention (see the 2026-09-07 tasks.kind/severity
migration's docstring for the crash-loop this guards against).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "403950e87a6f"
down_revision = "b3d7f4a2c8e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS signal_events")
    op.create_table(
        "signal_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("device_key", sa.String(length=64), nullable=True),
        sa.Column("device_name", sa.String(length=100), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sender_event_id", sa.String(length=200), nullable=True),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("DROP INDEX IF EXISTS ix_signal_events_source_device_occurred")
    op.create_index(
        "ix_signal_events_source_device_occurred",
        "signal_events", ["source", "device_key", "occurred_at"],
    )

    op.execute("DROP TABLE IF EXISTS watch_runs")
    op.create_table(
        "watch_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("watcher", sa.String(length=50), nullable=False),
        sa.Column("night_date", sa.String(length=10), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="watching"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frame_path", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("answer", JSONB(), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("checks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed", sa.Boolean(), nullable=True),
        sa.Column(
            "confirmed_by_user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.execute("DROP INDEX IF EXISTS ix_watch_runs_watcher_night")
    op.create_index(
        "ix_watch_runs_watcher_night", "watch_runs", ["watcher", "night_date"], unique=True,
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_watch_runs_watcher_night")
    op.drop_table("watch_runs")
    op.execute("DROP INDEX IF EXISTS ix_signal_events_source_device_occurred")
    op.drop_table("signal_events")
