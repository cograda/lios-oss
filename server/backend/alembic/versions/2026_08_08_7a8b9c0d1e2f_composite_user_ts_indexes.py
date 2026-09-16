"""Composite (user_id, ts) indexes: whatsapp_messages, mail_messages, vault_chunks

Hardening finding P2 (vault/Projects/lios/Plans/hardening-2026-08.md):
lastfm's `scrobbles` table already carries `ix_scrobbles_user_played_at`
(user_id, played_at) for its `WHERE user_id = ? ORDER BY <ts> DESC` recent-list
read. The same pattern exists via the `ListTool` DSL for:
  - `whatsapp_recent` -> WhatsAppMessage, ordered by `timestamp`
  - `gmail_recent` -> MailMessage, ordered by `date`
  - `vault_recent` -> VaultChunk, ordered by `modified_at`
each doing `scoped_query(session, Model)` (filters `user_id`) then
`.order_by(ts_col.desc())`. None had a composite index covering that
filter+sort together — `mail_messages` had only the bare `date` index added
under P1 (which still serves the unscoped freshness/sweep MAX(date) reads
and is left in place).

Revision ID: 7a8b9c0d1e2f
Revises: 6f7a8b9c0d1e
Create Date: 2026-08-08
"""

from alembic import op

revision = "7a8b9c0d1e2f"
down_revision = "6f7a8b9c0d1e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_whatsapp_messages_user_timestamp", "whatsapp_messages", ["user_id", "timestamp"],
    )
    op.create_index(
        "ix_mail_messages_user_date", "mail_messages", ["user_id", "date"],
    )
    op.create_index(
        "ix_vault_chunks_user_modified_at", "vault_chunks", ["user_id", "modified_at"],
    )


def downgrade() -> None:
    # IF EXISTS throughout — see user-memory `feedback_alembic_migration_safety`:
    # a partially-applied upgrade must still be reversible.
    op.drop_index("ix_vault_chunks_user_modified_at", table_name="vault_chunks", if_exists=True)
    op.drop_index("ix_mail_messages_user_date", table_name="mail_messages", if_exists=True)
    op.drop_index("ix_whatsapp_messages_user_timestamp", table_name="whatsapp_messages", if_exists=True)
