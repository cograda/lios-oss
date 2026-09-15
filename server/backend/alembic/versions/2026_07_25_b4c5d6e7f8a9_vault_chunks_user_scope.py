"""vault_chunks.user_id + per-user vault embeddings.

Vaults are one-per-user on disk (`/vaults/<user>/`) since 2026-06-05, but the
index was still household-shared: `vault_chunks` had no owner, and vault rows
in `embeddings` / `embedding_queue` carried `user_id IS NULL`, which the search
filter reads as "visible to everyone". A second user connecting would have had
`vault_search` return the first user's whole vault.

Existing vault data is all Alex's (user_id=1), so the backfill is a straight
assignment. `(user_id, path)` becomes the tracker's identity, replacing the
implicit assumption that `path` alone was unique.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The only user with vault data at the time of this migration.
_LEGACY_VAULT_OWNER = 1


def upgrade() -> None:
    # 1. Add nullable, backfill, then enforce NOT NULL — adding it non-null in
    #    one step would fail against the 338 existing rows.
    op.add_column("vault_chunks", sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute(f"UPDATE vault_chunks SET user_id = {_LEGACY_VAULT_OWNER} WHERE user_id IS NULL")
    op.alter_column("vault_chunks", "user_id", nullable=False)

    op.create_foreign_key(
        "fk_vault_chunks_user_id_users",
        "vault_chunks", "users",
        ["user_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_vault_chunks_user_id", "vault_chunks", ["user_id"])

    # 2. Identity is now (user_id, path). Deduplicate defensively first — a
    #    unique index cannot be built over duplicates, and a half-applied
    #    earlier run could have left some.
    op.execute(
        """
        DELETE FROM vault_chunks a
        USING vault_chunks b
        WHERE a.user_id = b.user_id AND a.path = b.path AND a.id > b.id
        """
    )
    #    Created as a CONSTRAINT, not a unique index, to match the ORM's
    #    UniqueConstraint — otherwise a migrated DB and a create_tables() DB
    #    disagree about what the object is, and downgrade breaks on one of them.
    op.create_unique_constraint(
        "uq_vault_chunks_user_path", "vault_chunks", ["user_id", "path"],
    )

    # 3. Claim the existing vault embeddings for the legacy owner. Without
    #    this they stay NULL = household-shared and remain readable by every
    #    user — the leak this migration exists to close.
    op.execute(
        f"UPDATE embeddings SET user_id = {_LEGACY_VAULT_OWNER} "
        "WHERE source = 'vault' AND user_id IS NULL"
    )
    op.execute(
        f"UPDATE embedding_queue SET user_id = {_LEGACY_VAULT_OWNER} "
        "WHERE source = 'vault' AND user_id IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE embedding_queue SET user_id = NULL "
        f"WHERE source = 'vault' AND user_id = {_LEGACY_VAULT_OWNER}"
    )
    op.execute(
        "UPDATE embeddings SET user_id = NULL "
        f"WHERE source = 'vault' AND user_id = {_LEGACY_VAULT_OWNER}"
    )
    # Drop the constraint first; the bare-index form only exists on a DB that
    # ran an older revision of this migration, so it's a belt-and-braces
    # fallback rather than the normal path.
    op.execute(
        "ALTER TABLE vault_chunks DROP CONSTRAINT IF EXISTS uq_vault_chunks_user_path"
    )
    op.execute("DROP INDEX IF EXISTS uq_vault_chunks_user_path")
    op.execute("DROP INDEX IF EXISTS ix_vault_chunks_user_id")
    op.execute(
        "ALTER TABLE vault_chunks DROP CONSTRAINT IF EXISTS fk_vault_chunks_user_id_users"
    )
    op.execute("ALTER TABLE vault_chunks DROP COLUMN IF EXISTS user_id")
