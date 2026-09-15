"""notification_sends.source / .status / .error_text — ledger every publish.

`vault/Projects/lios/Backlog.md`: "Ad-hoc pushes are never ledgered — 'was I
actually told?' is unanswerable". Only `sweep.py` wrote this table before
this change; `notify_send`, capture confirmations and every other direct
`client.publish()` caller left no trace (measured 2026-08-14: an ad-hoc send
left `notification_sends` at 233 rows before and after).

`fingerprint` becomes nullable — an ad-hoc send has nothing to dedupe
against, and Postgres treats NULLs as distinct under the existing partial
unique index (`ix_notification_sends_open_fingerprint`,
`WHERE resolved_at IS NULL`), so many NULL-fingerprint rows coexist freely
without touching that constraint.

`source` defaults to `'sweep'` on backfill because every existing row was
written by the sweep (the only prior writer); `status` defaults to `'sent'`
for the same reason — a sweep row's real per-send outcome already lives in
`send_count`/`last_sent_at`, unchanged by this migration.

Revision ID: a5c9e1b3f8d7
Revises: f2b6d8a1c4e7
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a5c9e1b3f8d7"
down_revision: Union[str, Sequence[str], None] = "f2b6d8a1c4e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "notification_sends", "fingerprint", existing_type=sa.String(length=255), nullable=True,
    )
    op.add_column(
        "notification_sends",
        sa.Column("source", sa.String(length=20), nullable=False, server_default="sweep"),
    )
    op.add_column(
        "notification_sends",
        sa.Column("status", sa.String(length=10), nullable=False, server_default="sent"),
    )
    op.add_column(
        "notification_sends",
        sa.Column("error_text", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # IF EXISTS throughout — a partially-applied upgrade must still be
    # reversible (same rule as every other migration in this package).
    op.execute("ALTER TABLE notification_sends DROP COLUMN IF EXISTS error_text")
    op.execute("ALTER TABLE notification_sends DROP COLUMN IF EXISTS status")
    op.execute("ALTER TABLE notification_sends DROP COLUMN IF EXISTS source")
    op.execute(
        "ALTER TABLE notification_sends ALTER COLUMN fingerprint SET NOT NULL"
    )
