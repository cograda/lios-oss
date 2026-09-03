"""Purge Syncthing .stversions rows from the vault index.

Revision ID: c4f1a9d7e2b8
Revises: 30877432d102
Create Date: 2026-08-14

The walker never excluded `.stversions/`, Syncthing's rolling snapshot of every
save of every file, so version history was indexed alongside the live vault.

This is a data cleanup, not a schema change. It exists as a migration rather
than a hand-run SQL snippet because the fix is only complete once the rows are
gone from the deployed database, and a migration is the one path that is
guaranteed to run there exactly once.

Why it matters enough to purge rather than leave to age out: every superseded
revision competes with its own live file for the same query. Measured
2026-08-13, the query "Zappi CT clamp solar and Huawei installer access Modbus"
returned a 1 August snapshot at 0.7651 *above* the live file it is a snapshot of
at 0.7600.

Scope: `embeddings` rows cascade to every per-space vector table via
`ON DELETE CASCADE` on `embedding_id`, so deleting there also clears
`embedding_vec_gemini_1536` and `embedding_vec_bge_small_384` without naming
them — which is what keeps this correct when a third space is added.

Irreversible by design: `downgrade()` is a no-op. The rows were junk, we do not
keep a copy, and re-indexing the live vault would not recreate them anyway
because the walker now excludes the directory.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c4f1a9d7e2b8"
down_revision: Union[str, Sequence[str], None] = "30877432d102"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A vault chunk's source_id is either the bare relative path or `{path}#{n}`,
# so a prefix match on the directory catches both. `.` and `/` are literals in
# LIKE; there is no `_` or `%` in the prefix, so no ESCAPE clause is needed.
_PREFIX = ".stversions/%"


def upgrade() -> None:
    # Order matters only for readability — there are no FKs between these three
    # and the vault_chunks tracking row is independent of the embedding rows.
    op.execute(
        f"DELETE FROM embeddings WHERE source = 'vault' AND source_id LIKE '{_PREFIX}'"
    )
    op.execute(
        f"DELETE FROM embedding_queue WHERE source = 'vault' AND source_id LIKE '{_PREFIX}'"
    )
    op.execute(f"DELETE FROM vault_chunks WHERE path LIKE '{_PREFIX}'")


def downgrade() -> None:
    """No-op. Deleted junk is not restorable and should not be."""
