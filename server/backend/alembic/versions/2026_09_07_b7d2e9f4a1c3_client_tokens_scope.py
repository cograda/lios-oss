"""client_tokens.scope — a bearer that can read but never write

Revision ID: b7d2e9f4a1c3
Revises: e5f9c3a7b8d2
Create Date: 2026-09-07

The Hall Panel (apps/hub, a wall touchscreen children use) needs a bearer.
Its docs said `HUB_COMAR_TOKEN` was a "device principal" whose capability
whitelist was "enforced inside comar's registry dispatch" — that was never
implemented: every bearer carried its user's full authority, and the
2026-09-06 scoping audit recorded it as "recorded, not fixed".

This column is the fix's storage half. `scope` is `'full'` (unchanged
behaviour, the default for every existing row and for `.mint()`) or
`'readonly'`: a read-only bearer may call tools whose MCP annotations
declare `readOnlyHint: true` (`app/plugin/dispatch.py`) and make `GET`
requests plus `POST /api/v1/tools/{name}` (`app/auth/client_token.py`);
everything else is refused. The vocabulary is closed by the minting route,
not by a CHECK constraint — `client_tokens` is per-user, and the scoping
canary (tests/test_user_scoping.py) seeds a marker into every string column
of every per-user table, which a database-level vocabulary would reject.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d2e9f4a1c3"
down_revision: Union[str, Sequence[str], None] = "e5f9c3a7b8d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "client_tokens",
        sa.Column(
            "scope", sa.String(length=16), nullable=False, server_default="full",
        ),
    )


def downgrade() -> None:
    # IF EXISTS: a downgrade re-run after a half-applied step must not fail
    # on the very object it is there to remove.
    op.execute("ALTER TABLE client_tokens DROP COLUMN IF EXISTS scope")
