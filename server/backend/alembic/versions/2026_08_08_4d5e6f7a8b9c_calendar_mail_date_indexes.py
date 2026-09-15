"""calendar_events.start_time / mail_messages.date indexes

Hardening finding P1 (vault/Projects/lios/Plans/hardening-2026-08.md):
both columns are filtered/ordered on constantly (calendar tools' date-range
queries, mail's recent/list `ORDER BY date DESC`) but carried no index beyond
SourcedRecordMixin's.

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-08-08
"""

from alembic import op

revision = "4d5e6f7a8b9c"
down_revision = "3c4d5e6f7a8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_calendar_events_start_time", "calendar_events", ["start_time"],
    )
    op.create_index(
        "ix_mail_messages_date", "mail_messages", ["date"],
    )


def downgrade() -> None:
    # IF EXISTS throughout — see user-memory `feedback_alembic_migration_safety`:
    # a partially-applied upgrade must still be reversible.
    op.drop_index("ix_mail_messages_date", table_name="mail_messages", if_exists=True)
    op.drop_index("ix_calendar_events_start_time", table_name="calendar_events", if_exists=True)
