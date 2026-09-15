"""token_hardening: hash tokens at rest, add expiry, transient install plaintext

V4 chunk 2.3. `client_tokens.token` and `mcp_access_tokens.{access_token,
refresh_token}` were plaintext, looked up by SQL equality — a DB read
(backup leak, careless `SELECT *`, SQL injection) was a full impersonation
of every user. This migration:

  - adds `token_hash`/`token_last4` (client_tokens) and
    `access_token_hash`/`access_token_last4`/`refresh_token_hash`/
    `refresh_token_last4` (mcp_access_tokens), backfills them by hashing
    the existing plaintext values, then drops the plaintext columns
  - adds `client_tokens.expires_at` — a sliding 180-day TTL, backfilled to
    "180 days from now" so existing devices don't go instantly stale on
    deploy (see app.auth.client_token.resolve_token_to_user for the slide)
  - adds `install_codes.token_plaintext` — with `client_tokens.token` gone,
    the bearer an in-flight install will use has nowhere else to live
    between mint time and redemption (up to 24h later); see
    app/models/clients.py::InstallCode's docstring for the reasoning

Downgrade regenerates fresh random plaintext for existing rows — a sha256
hash is one-way, so the original bearer values are gone by construction
and cannot be recovered. Any device using this migration's hashed tokens
will need re-issued credentials after a downgrade; that's an accepted,
unavoidable cost of ever running this migration in reverse.

See app/auth/hashing.py, app/models/clients.py, app/models/oauth_clients.py.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_TOKEN_TTL_DAYS = 180


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    default_expiry = now + timedelta(days=_DEFAULT_TOKEN_TTL_DAYS)

    # ------------------------------------------------------------------
    # client_tokens: token -> token_hash/token_last4, + expires_at
    # ------------------------------------------------------------------
    op.add_column("client_tokens", sa.Column("token_hash", sa.String(64), nullable=True))
    op.add_column("client_tokens", sa.Column("token_last4", sa.String(4), nullable=True))
    op.add_column("client_tokens", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))

    rows = bind.execute(sa.text("SELECT id, token FROM client_tokens")).fetchall()
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE client_tokens SET token_hash=:h, token_last4=:l4, "
                "expires_at=:exp WHERE id=:id"
            ),
            {
                "h": _sha256_hex(row.token),
                "l4": row.token[-4:],
                "exp": default_expiry,
                "id": row.id,
            },
        )

    op.alter_column("client_tokens", "token_hash", nullable=False)
    op.alter_column("client_tokens", "token_last4", nullable=False)

    # Pre-alembic table (created via create_tables()/metadata, not a
    # migration) — SQLAlchemy's default naming for a plain `unique=True,
    # index=True` column, hence the guarded raw-SQL drop rather than
    # op.drop_constraint with a known name.
    op.execute("ALTER TABLE client_tokens DROP CONSTRAINT IF EXISTS client_tokens_token_key")
    op.execute("DROP INDEX IF EXISTS ix_client_tokens_token")
    op.drop_column("client_tokens", "token")

    op.create_index(
        "ix_client_tokens_token_hash", "client_tokens", ["token_hash"], unique=True,
    )

    # ------------------------------------------------------------------
    # mcp_access_tokens: access_token/refresh_token -> *_hash/*_last4
    # ------------------------------------------------------------------
    op.add_column("mcp_access_tokens", sa.Column("access_token_hash", sa.String(64), nullable=True))
    op.add_column("mcp_access_tokens", sa.Column("access_token_last4", sa.String(4), nullable=True))
    op.add_column("mcp_access_tokens", sa.Column("refresh_token_hash", sa.String(64), nullable=True))
    op.add_column("mcp_access_tokens", sa.Column("refresh_token_last4", sa.String(4), nullable=True))

    rows = bind.execute(
        sa.text("SELECT id, access_token, refresh_token FROM mcp_access_tokens")
    ).fetchall()
    for row in rows:
        refresh_hash = _sha256_hex(row.refresh_token) if row.refresh_token else None
        refresh_last4 = row.refresh_token[-4:] if row.refresh_token else None
        bind.execute(
            sa.text(
                "UPDATE mcp_access_tokens SET access_token_hash=:ah, "
                "access_token_last4=:al4, refresh_token_hash=:rh, "
                "refresh_token_last4=:rl4 WHERE id=:id"
            ),
            {
                "ah": _sha256_hex(row.access_token),
                "al4": row.access_token[-4:],
                "rh": refresh_hash,
                "rl4": refresh_last4,
                "id": row.id,
            },
        )

    op.alter_column("mcp_access_tokens", "access_token_hash", nullable=False)
    op.alter_column("mcp_access_tokens", "access_token_last4", nullable=False)

    # This table WAS created via an explicit migration (2026_06_08), so
    # the constraint/index names are known.
    op.execute(
        "ALTER TABLE mcp_access_tokens DROP CONSTRAINT IF EXISTS uq_mcp_access_tokens_access_token"
    )
    op.execute(
        "ALTER TABLE mcp_access_tokens DROP CONSTRAINT IF EXISTS uq_mcp_access_tokens_refresh_token"
    )
    op.execute("DROP INDEX IF EXISTS ix_mcp_access_tokens_access_token")
    op.execute("DROP INDEX IF EXISTS ix_mcp_access_tokens_refresh_token")
    op.drop_column("mcp_access_tokens", "access_token")
    op.drop_column("mcp_access_tokens", "refresh_token")

    op.create_index(
        "ix_mcp_access_tokens_access_token_hash", "mcp_access_tokens",
        ["access_token_hash"], unique=True,
    )
    op.create_index(
        "ix_mcp_access_tokens_refresh_token_hash", "mcp_access_tokens",
        ["refresh_token_hash"], unique=True,
    )

    # ------------------------------------------------------------------
    # install_codes: transient plaintext holding place (see clients.py)
    # ------------------------------------------------------------------
    op.add_column("install_codes", sa.Column("token_plaintext", sa.String(64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_column("install_codes", "token_plaintext")

    op.execute("DROP INDEX IF EXISTS ix_mcp_access_tokens_refresh_token_hash")
    op.execute("DROP INDEX IF EXISTS ix_mcp_access_tokens_access_token_hash")

    op.add_column("mcp_access_tokens", sa.Column("access_token", sa.String(128), nullable=True))
    op.add_column("mcp_access_tokens", sa.Column("refresh_token", sa.String(128), nullable=True))

    # Hashes are one-way — regenerate fresh random values rather than
    # pretend we can recover the originals. See module docstring.
    rows = bind.execute(sa.text("SELECT id FROM mcp_access_tokens")).fetchall()
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE mcp_access_tokens SET access_token=:a, refresh_token=:r WHERE id=:id"
            ),
            {"a": secrets.token_urlsafe(32), "r": secrets.token_urlsafe(32), "id": row.id},
        )
    op.alter_column("mcp_access_tokens", "access_token", nullable=False)

    op.create_unique_constraint(
        "uq_mcp_access_tokens_access_token", "mcp_access_tokens", ["access_token"],
    )
    op.create_unique_constraint(
        "uq_mcp_access_tokens_refresh_token", "mcp_access_tokens", ["refresh_token"],
    )
    op.create_index(
        "ix_mcp_access_tokens_access_token", "mcp_access_tokens", ["access_token"], unique=False,
    )
    op.create_index(
        "ix_mcp_access_tokens_refresh_token", "mcp_access_tokens", ["refresh_token"], unique=False,
    )

    op.drop_column("mcp_access_tokens", "access_token_last4")
    op.drop_column("mcp_access_tokens", "access_token_hash")
    op.drop_column("mcp_access_tokens", "refresh_token_last4")
    op.drop_column("mcp_access_tokens", "refresh_token_hash")

    op.add_column("client_tokens", sa.Column("token", sa.String(64), nullable=True))
    rows = bind.execute(sa.text("SELECT id FROM client_tokens")).fetchall()
    for row in rows:
        bind.execute(
            sa.text("UPDATE client_tokens SET token=:t WHERE id=:id"),
            {"t": secrets.token_hex(32), "id": row.id},
        )
    op.alter_column("client_tokens", "token", nullable=False)
    op.create_index("ix_client_tokens_token", "client_tokens", ["token"], unique=True)

    op.execute("DROP INDEX IF EXISTS ix_client_tokens_token_hash")
    op.drop_column("client_tokens", "expires_at")
    op.drop_column("client_tokens", "token_last4")
    op.drop_column("client_tokens", "token_hash")
