"""historical_documents.owner_user_id — private documents in a shared corpus

Revision ID: d4e8b1f7a2c6
Revises: a3f7c2d9e1b4
Create Date: 2026-09-06

The decision this serves (Alex, 2026-09-06): his claude.ai conversation
history is his, not the household's. Until now `historical_documents` had no
owner column, every corpus chunk was embedded with `embeddings.user_id NULL`
(household-shared), and `claude_history_search` / `corpus_search(
source_types=["claude_conversation"])` returned his 5,000 conversations to
any caller.

Two parts:

  1. Schema: one nullable FK column. NULL keeps today's meaning — shared —
     so manuals, renovation paperwork and the comms archive are untouched.

  2. Data fix for the rows that already exist. The column alone changes
     nothing a search can see; what scopes a search is `embeddings.user_id`.
     So every existing `claude_conversation` document is stamped owner = 1
     (alex — the seed user ids are fixed: alex=1, sam=2), and the embedding
     rows for its chunks (source 'historical_corpus', source_id
     '{document_id}:{chunk_index}' — see ingest._upsert_document) move from
     NULL to user 1, along with any still-pending queue rows so the worker
     embeds them as his. `split_part(source_id, ':', 1)` is compared as text
     against `historical_documents.id::text`, never cast — within
     source='historical_corpus' every source_id has that shape, but a text
     comparison stays safe even if a stray row does not.

The SQL is exposed as module constants so a test can run exactly these
statements against a seeded database and report the row counts, rather than
a re-typed approximation of them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e8b1f7a2c6"
down_revision: Union[str, Sequence[str], None] = "a3f7c2d9e1b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Alex's user id. The seed migration (2026_05_02) fixes alex=1, sam=2 and
# every per-user test binds user 1 as him; this is that same constant, not a
# guess at a name.
CLAUDE_HISTORY_OWNER_ID = 1
PRIVATE_SOURCE_TYPE = "claude_conversation"

STAMP_DOCUMENTS_SQL = f"""
UPDATE historical_documents
   SET owner_user_id = {CLAUDE_HISTORY_OWNER_ID}
 WHERE source_type = '{PRIVATE_SOURCE_TYPE}'
   AND owner_user_id IS NULL
"""

STAMP_EMBEDDINGS_SQL = f"""
UPDATE embeddings e
   SET user_id = {CLAUDE_HISTORY_OWNER_ID}
  FROM historical_documents d
 WHERE e.source = 'historical_corpus'
   AND e.user_id IS NULL
   AND d.source_type = '{PRIVATE_SOURCE_TYPE}'
   AND split_part(e.source_id, ':', 1) = d.id::text
"""

STAMP_QUEUE_SQL = f"""
UPDATE embedding_queue q
   SET user_id = {CLAUDE_HISTORY_OWNER_ID}
  FROM historical_documents d
 WHERE q.source = 'historical_corpus'
   AND q.user_id IS NULL
   AND d.source_type = '{PRIVATE_SOURCE_TYPE}'
   AND split_part(q.source_id, ':', 1) = d.id::text
"""

# Order matters only for readability — the three statements are independent
# (the embedding/queue updates key on source_type, not on the new column).
DATA_FIX_SQL = (STAMP_DOCUMENTS_SQL, STAMP_EMBEDDINGS_SQL, STAMP_QUEUE_SQL)


def upgrade() -> None:
    op.add_column(
        "historical_documents",
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_historical_documents_owner_user_id_users",
        "historical_documents", "users", ["owner_user_id"], ["id"],
    )
    op.create_index(
        "ix_historical_documents_owner_user_id",
        "historical_documents", ["owner_user_id"],
    )
    for statement in DATA_FIX_SQL:
        op.execute(statement)


def downgrade() -> None:
    # The embeddings are left as user 1's: a downgrade removes the record of
    # the decision, not the privacy it granted — un-hiding the conversations
    # is a deliberate act, not a side effect of rolling a schema back.
    # IF EXISTS throughout: a downgrade re-run after a half-applied step must
    # not fail on the very objects it is there to remove.
    op.execute("DROP INDEX IF EXISTS ix_historical_documents_owner_user_id")
    op.execute(
        "ALTER TABLE historical_documents "
        "DROP CONSTRAINT IF EXISTS fk_historical_documents_owner_user_id_users"
    )
    op.execute("ALTER TABLE historical_documents DROP COLUMN IF EXISTS owner_user_id")
