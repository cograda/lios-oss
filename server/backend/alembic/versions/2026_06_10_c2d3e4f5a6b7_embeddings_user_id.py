"""embeddings_user_id: per-user scoping for the unified embedding store

The 2026-05-02 multi-user migration added user_id to the source tables
(mail_messages, whatsapp_messages) but not to the derived embeddings /
embedding_queue tables, so semantic search returned both users' email and
WhatsApp content to either user. Adds nullable user_id (NULL = household-
shared: vault, historical_corpus, coffee) and backfills ownership:

  email     — join mail_messages on google_message_id
  whatsapp  — segment ids are "{chat_id}:{start_epoch}:{end_epoch}";
              extract chat_id and join to the chat's owning user
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("embeddings", "embedding_queue"):
        op.add_column(table, sa.Column("user_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_user_id_users",
            table,
            "users",
            ["user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])

    # Backfill email embeddings from the owning cached message.
    # (google_message_id is unique per user; if the same id ever existed in
    # both users' caches the row gets one of them — re-embedding corrects it.)
    for table in ("embeddings", "embedding_queue"):
        op.execute(sa.text(f"""
            UPDATE {table} e
            SET user_id = m.user_id
            FROM mail_messages m
            WHERE e.source = 'email'
              AND e.user_id IS NULL
              AND e.source_id = m.google_message_id
        """))

    # Backfill whatsapp segments via chat ownership. Segment ids are
    # "{chat_id}:{start}:{end}"; chat JIDs contain no colons.
    for table in ("embeddings", "embedding_queue"):
        op.execute(sa.text(f"""
            UPDATE {table} e
            SET user_id = owners.user_id
            FROM (
                SELECT chat_id, MIN(user_id) AS user_id
                FROM whatsapp_messages
                GROUP BY chat_id
            ) owners
            WHERE e.source = 'whatsapp'
              AND e.user_id IS NULL
              AND substring(e.source_id FROM '^(.*):[0-9]+:[0-9]+$') = owners.chat_id
        """))

    # Fail closed: per-user rows the backfill couldn't attribute would
    # otherwise stay NULL (= shared = still leaky). Delete them — the next
    # gmail/whatsapp embed run re-enqueues them with the correct owner.
    for table in ("embeddings", "embedding_queue"):
        op.execute(sa.text(f"""
            DELETE FROM {table}
            WHERE source IN ('email', 'whatsapp')
              AND user_id IS NULL
        """))


def downgrade() -> None:
    for table in ("embeddings", "embedding_queue"):
        op.execute(sa.text(f"DROP INDEX IF EXISTS ix_{table}_user_id"))
        op.execute(sa.text(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_user_id_users"
        ))
        op.drop_column(table, "user_id")
