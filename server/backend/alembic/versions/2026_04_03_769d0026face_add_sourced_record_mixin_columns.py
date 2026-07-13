"""add sourced record mixin columns

Add source_id, source_ts, content_hash to 8 integration tables.
synced_at is added only where it doesn't already exist.
Backfills source_id and source_ts from existing domain-specific columns.

Revision ID: 769d0026face
Revises: d9978698c425
Create Date: 2026-04-03 22:23:17.945925

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '769d0026face'
down_revision: Union[str, Sequence[str], None] = 'd9978698c425'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that get the mixin, and how their existing columns map.
# Format: (table, source_id_from, source_ts_from, has_synced_at)
TABLES = [
    ("calendar_events",      "google_event_id",   "start_time",  True),
    ("mail_messages",        "google_message_id",  "date",        True),
    ("whatsapp_messages",    "message_id",         "timestamp",   False),
    ("scrobbles",            None,                 "played_at",   True),   # composite key, no single source_id
    ("reminders",            "uid",                "due_date",    True),
    ("health_workouts",      "uid",                "start_time",  True),
    ("health_sleep_sessions", "uid",               "start_time",  True),
    ("vault_chunks",         "path",               "modified_at", False),  # has indexed_at, not synced_at
]


def upgrade() -> None:
    for table, source_id_from, source_ts_from, has_synced_at in TABLES:
        # Add source_id (nullable, indexed)
        op.add_column(table, sa.Column("source_id", sa.String(500), nullable=True))
        op.create_index(f"ix_{table}_source_id", table, ["source_id"])

        # Add source_ts (nullable, indexed)
        op.add_column(table, sa.Column("source_ts", sa.DateTime(timezone=True), nullable=True))
        op.create_index(f"ix_{table}_source_ts", table, ["source_ts"])

        # Add synced_at only if the table doesn't have it
        if not has_synced_at:
            op.add_column(table, sa.Column(
                "synced_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False,
            ))

        # Add content_hash (nullable)
        op.add_column(table, sa.Column("content_hash", sa.String(64), nullable=True))

    # Backfill source_id from existing columns
    for table, source_id_from, source_ts_from, _ in TABLES:
        if source_id_from:
            op.execute(
                sa.text(f'UPDATE "{table}" SET source_id = {source_id_from} WHERE source_id IS NULL')
            )
        if source_ts_from:
            op.execute(
                sa.text(f'UPDATE "{table}" SET source_ts = {source_ts_from} WHERE source_ts IS NULL')
            )


def downgrade() -> None:
    for table, _, _, has_synced_at in TABLES:
        op.drop_index(f"ix_{table}_source_ts", table_name=table)
        op.drop_column(table, "source_ts")
        op.drop_index(f"ix_{table}_source_id", table_name=table)
        op.drop_column(table, "source_id")
        op.drop_column(table, "content_hash")
        if not has_synced_at:
            op.drop_column(table, "synced_at")
