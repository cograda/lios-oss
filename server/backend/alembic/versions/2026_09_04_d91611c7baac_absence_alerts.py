"""Absence alerts (R5, Wave 2) — one persisted row per distinct absence finding

`AbsenceAlert` (app/integrations/tasks/models.py) is the dedup ledger for
absence detection (app/integrations/tasks/absence.py): a routine whose window
closed with no round completed, a `waiting` task past due with no note, or a
snag unanswered for weeks. `dedup_key` identifies one distinct finding
(`absence:<kind>:<ref>:<since-iso>`); the partial unique index below allows
at most one OPEN (`resolved_at IS NULL`) row per key, the same shape as
`notification_sends`' open-fingerprint index.

Household-shared, following `notification_sends` and every other table in
`tasks`/`snags`: no `UserOwnedMixin` — `owner_id` is a plain nullable FK
(NULL for a household-shared finding, e.g. an unowned snag).

Revision ID: d91611c7baac
Revises: a5c9e1b3f8d7
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa

revision = "d91611c7baac"
down_revision = "a5c9e1b3f8d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "absence_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dedup_key", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("ref", sa.String(20), nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_absence_alerts_dedup_key", "absence_alerts", ["dedup_key"])
    op.create_index("ix_absence_alerts_ref", "absence_alerts", ["ref"])
    op.create_index("ix_absence_alerts_owner_id", "absence_alerts", ["owner_id"])
    op.create_index("ix_absence_alerts_resolved_at", "absence_alerts", ["resolved_at"])
    op.create_index(
        "ix_absence_alerts_open_dedup_key", "absence_alerts", ["dedup_key"],
        unique=True, postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_absence_alerts_open_dedup_key")
    op.execute("DROP INDEX IF EXISTS ix_absence_alerts_resolved_at")
    op.execute("DROP INDEX IF EXISTS ix_absence_alerts_owner_id")
    op.execute("DROP INDEX IF EXISTS ix_absence_alerts_ref")
    op.execute("DROP INDEX IF EXISTS ix_absence_alerts_dedup_key")
    op.drop_table("absence_alerts")
