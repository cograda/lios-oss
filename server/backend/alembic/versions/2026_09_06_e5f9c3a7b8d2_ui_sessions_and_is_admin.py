"""users.is_admin + ui_sessions — the dashboard signs in a person, not a password

Revision ID: e5f9c3a7b8d2
Revises: d4e8b1f7a2c6
Create Date: 2026-09-06

The decision this serves (Alex, 2026-09-06): lios has ONE credential, the
per-user bearer. The admin dashboard was the last thing authenticating with
a shared secret (`HOME_UI_TOKEN`, a password in a cookie that named nobody).
It now signs in with a person's own bearer and holds a server-side session.

Two parts:

  1. `users.is_admin` — the only role concept there is. Default false; the
     data fix stamps user 1 / name 'alex' true (the seed migration fixes
     alex=1, sam=2, the same constant every other data fix uses). Admin
     gates the routes that mint bearers, purge data, change integration
     config, and read other people's logs/preferences — see
     `app/auth/ui_session.py` for the classification.

  2. `ui_sessions` — one row per signed-in browser: a hashed random id (the
     cookie value is never stored), the user, the bearer that signed it in,
     and a sliding expiry. Revoking the bearer or deactivating the user
     invalidates the session on the next request, not at some later login.

There is no migration of the old `ui_token` cookie: it named no user, so
there is nothing to migrate it *to*. After deploy the operator signs in once
with his bearer.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f9c3a7b8d2"
down_revision: Union[str, Sequence[str], None] = "d4e8b1f7a2c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Alex's user id — the seed migration (2026_05_02) fixes alex=1, sam=2.
ADMIN_USER_ID = 1
ADMIN_USER_NAME = "alex"

STAMP_ADMIN_SQL = f"""
UPDATE users
   SET is_admin = true
 WHERE id = {ADMIN_USER_ID} OR name = '{ADMIN_USER_NAME}'
"""


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false"),
        ),
    )
    op.execute(STAMP_ADMIN_SQL)

    op.create_table(
        "ui_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("client_token_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["client_token_id"], ["client_tokens.id"], ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_ui_sessions_session_hash", "ui_sessions", ["session_hash"], unique=True,
    )
    op.create_index("ix_ui_sessions_user_id", "ui_sessions", ["user_id"])
    op.create_index("ix_ui_sessions_client_token_id", "ui_sessions", ["client_token_id"])


def downgrade() -> None:
    # IF EXISTS throughout: a downgrade re-run after a half-applied step must
    # not fail on the very objects it is there to remove.
    op.execute("DROP INDEX IF EXISTS ix_ui_sessions_client_token_id")
    op.execute("DROP INDEX IF EXISTS ix_ui_sessions_user_id")
    op.execute("DROP INDEX IF EXISTS ix_ui_sessions_session_hash")
    op.execute("DROP TABLE IF EXISTS ui_sessions")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_admin")
