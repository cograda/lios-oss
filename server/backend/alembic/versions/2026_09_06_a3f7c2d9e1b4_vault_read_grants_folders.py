"""vault_read_grants.folders — folder-scoped cross-user vault reads

Revision ID: a3f7c2d9e1b4
Revises: c7a3f9e2b5d1
Create Date: 2026-09-06

Adds one nullable JSONB column. NULL keeps today's meaning — the whole vault —
so every existing grant row behaves exactly as it did before this migration;
nothing is backfilled and no grant is created or narrowed here. Folder scope
is set explicitly with `python -m app.scripts.grant_vault_read --folders …`,
because an access decision does not belong in `alembic upgrade head`.

The decision this serves (2026-09-06): Sam reads the renovation and
household folders of Alex's vault as shared context rather than a copy, and
`Health/`, `Notes/`, `Blog/`, `Daily Notes/` are never in that scope.
Enforcement is `app/services/vault_scope.py`, not this column — the column
only records which prefixes a grant opens.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a3f7c2d9e1b4"
down_revision: Union[str, Sequence[str], None] = "c7a3f9e2b5d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vault_read_grants",
        sa.Column("folders", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    # IF EXISTS: a downgrade re-run after a half-applied step must not fail
    # on the very column it is there to remove.
    op.execute("ALTER TABLE vault_read_grants DROP COLUMN IF EXISTS folders")
