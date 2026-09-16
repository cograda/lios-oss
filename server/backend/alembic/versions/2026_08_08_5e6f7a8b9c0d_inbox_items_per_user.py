"""inbox_items — per-user ownership ledger for the inbox integration (F6).

Security finding F6 (vault/Projects/lios/Plans/hardening-2026-08.md,
recorded 2026-08-08 with Alex): the inbox had no user scoping at all — items
lived in a flat on-disk tree and any valid client token could see or route
everything via `inbox_pending`/`inbox_preview`/etc. and the routing tools,
including voice memos, the likeliest personal crossover in the system.

`inbox_items` is a brand-new table, not an existing one gaining a column, so
there is no SQL-level backfill step here (contrast
`2026_07_25_b4c5d6e7f8a9_vault_chunks_user_scope.py` or
`2026_08_07_0f1e2d3c4b5a_whatsapp_contacts_per_user.py`, which had to
add-nullable/backfill/set-not-null against rows that already existed):
`user_id` is `NOT NULL` from creation because the table starts empty. The
equivalent of "existing rows belong to user 1" for inbox is a filesystem-and-
data operation on the pre-existing flat `/inbox/<bucket>/` tree, not a schema
migration — see `app/integrations/inbox/scan.py::adopt_legacy_files()`, which
lazily and idempotently moves those files into user 1's subtree and inserts
the corresponding `InboxItem` rows the first time anything reads the inbox
after this migration ships.

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-08-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5e6f7a8b9c0d"
down_revision: Union[str, Sequence[str], None] = "4d5e6f7a8b9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbox_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.UniqueConstraint("user_id", "relative_path", name="uq_inbox_items_user_relpath"),
    )
    op.create_index("ix_inbox_items_user_id", "inbox_items", ["user_id"])
    op.create_index("ix_inbox_items_sha256", "inbox_items", ["sha256"])


def downgrade() -> None:
    # IF EXISTS throughout — see user-memory `feedback_alembic_migration_safety`:
    # a partially-applied upgrade must still be reversible.
    op.execute("DROP INDEX IF EXISTS ix_inbox_items_sha256")
    op.execute("DROP INDEX IF EXISTS ix_inbox_items_user_id")
    op.execute("ALTER TABLE inbox_items DROP CONSTRAINT IF EXISTS uq_inbox_items_user_relpath")
    op.execute("DROP TABLE IF EXISTS inbox_items")
