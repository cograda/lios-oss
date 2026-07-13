"""fold_inline_schema_migrations: retire main.py's parallel migration path

_run_schema_migrations() in main.py added sync_state columns with inline
ALTERs on every startup — a second migration mechanism alongside alembic.
This revision absorbs those ALTERs (IF NOT EXISTS — the live DB already
has them; fresh DBs get them via create_all) so alembic is the only
schema authority.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS last_sync_duration_ms INTEGER"
    ))
    op.execute(sa.text(
        "ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0"
    ))


def downgrade() -> None:
    # Pre-dated this revision in production; nothing to undo.
    pass
