"""historical_documents.project_tags — generic server_default.

Part of separating platform code from personalisation (2026-07-28). The
column's server_default was the literal `{riverside}` — a family renovation
project name baked into the schema. The default now comes from the
`historical_corpus.default_project_tag` config key, which the app reads on
every ingest (`ingest.default_project_tags()`), so this server_default is
only a belt-and-braces floor for hand-written INSERTs.

**Existing rows are deliberately not rewritten.** Documents already tagged
`riverside` were ingested under that project and the tag is a true
contemporaneous record — the same forward-only rename convention the vault
uses for the Riverside -> Comar house rename. This migration changes only the
default for future inserts.

Column-default-only change: no table rewrite, no lock beyond the brief
ACCESS EXCLUSIVE that ALTER COLUMN SET DEFAULT takes on the catalog.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "historical_documents"
_COLUMN = "project_tags"
_OLD_DEFAULT = "{riverside}"
_NEW_DEFAULT = "{household}"


def upgrade() -> None:
    op.alter_column(
        _TABLE, _COLUMN,
        server_default=_NEW_DEFAULT,
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        _TABLE, _COLUMN,
        server_default=_OLD_DEFAULT,
        existing_nullable=False,
    )
