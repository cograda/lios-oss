"""mail_messages: rfc_message_id, body_text, is_personal — for the timemachine import

Three additive, nullable columns on `mail_messages`. Nothing existing is
rewritten, so this is safe against prod state as-is.

Why each one exists:

* `rfc_message_id` — the RFC 5322 `Message-ID` header. `google_message_id` is
  Gmail's *API* id, which no archive format carries: a Takeout mbox (and hence
  timemachine, which was parsed from one) has only `Message-ID`, `X-GM-THRID`
  and `X-Gmail-Labels`. Without a header-derived key the two sources cannot be
  reconciled at all — a later full API backfill would re-insert every archived
  message as new, with no way to detect it. Indexed, deliberately not unique:
  duplicates are legal in mail (the same message can appear in Sent and in a
  list copy), and a unique constraint would fail the import rather than surface
  the duplicate.

* `body_text` — the message body. Today bodies are fetched from the Gmail API
  at embed time and kept only inside `embeddings.chunk_text`, truncated to
  4000 chars, so the corpus can never be re-chunked or re-embedded without
  going back to Google. Storing it makes the archive self-sufficient.

* `is_personal` — timemachine's human-vs-bulk classification. 26k of its 127k
  Gmail messages are personal; the rest are receipts, newsletters and
  notifications. It gates what gets embedded, keeping bulk mail keyword-
  searchable without letting it swamp semantic search.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mail_messages", sa.Column("rfc_message_id", sa.String(length=998), nullable=True))
    op.add_column("mail_messages", sa.Column("body_text", sa.Text(), nullable=True))
    op.add_column("mail_messages", sa.Column("is_personal", sa.Boolean(), nullable=True))
    op.create_index(
        "ix_mail_messages_rfc_message_id", "mail_messages", ["rfc_message_id"], unique=False
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_mail_messages_rfc_message_id")
    for col in ("is_personal", "body_text", "rfc_message_id"):
        op.execute(f"ALTER TABLE mail_messages DROP COLUMN IF EXISTS {col}")
