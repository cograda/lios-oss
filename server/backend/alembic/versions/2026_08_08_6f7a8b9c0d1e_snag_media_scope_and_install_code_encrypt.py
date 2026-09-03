"""snag_source_messages per-user + install_codes.token_plaintext encrypted.

Hardening findings F5 and F4 (vault/Projects/lios/Plans/hardening-2026-08.md):

F5 — `snag_source_messages.message_ref` was globally unique, so a group-chat
message ingested by BOTH household members' WhatsApp bridges shared a
`message_ref` across their two `whatsapp_messages` rows. That meant the
second user's `snag_capture` run saw the ref as "already captured" (by the
first user) and silently skipped it, even though it was a new capture from
their own side. Fixes here:
  - `snag_source_messages` becomes per-user (`UserOwnedMixin`): the unique
    constraint moves from bare `message_ref` to `(user_id, message_ref)`.
    Existing rows backfill to user_id=1 (the only user with data at the
    time this table existed) since Sam's WhatsApp bridge had not yet
    ingested anything captured into a snag.
  - `created_at` added — every other UserOwnedMixin table in this schema
    carries a DateTime column (the generic scoping test suite in
    `tests/test_user_scoping.py` asserts this), and this table previously
    had none.
  - `snags` itself is untouched — it stays household-shared by design
    (renovation is joint work), only the per-message idempotency
    bookkeeping is per-user.

F4 — `install_codes.token_plaintext` held a live bearer token in cleartext
at rest for its 24h validity window. Encrypted with the same Fernet
machinery already used for `oauth_tokens`/`integration_config`
(`app.auth.encryption`) — write side is `app/scripts/create_install_code.py`,
read side is `app/routes/install.py`. Column widens `String(64)` -> `Text`
since Fernet ciphertext of a 64-char hex token is well over 64 characters.
Existing rows: an install code's window is 24h and single-use, so rather
than migrate plaintext values through the new encryption in-place (which
would need the Fernet key available at migration time, and buys nothing —
any not-yet-redeemed code is expired within a day regardless), this
migration simply NULLs any still-present plaintext. A code with a nulled
token_plaintext hits the existing `"Install token no longer available"`
409 path in `routes/install.py` on redemption, which is the same failure
mode an already-redeemed or expired code produces today.

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-08-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6f7a8b9c0d1e"
down_revision: Union[str, Sequence[str], None] = "5e6f7a8b9c0d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The only user with snag_source_messages data at the time of this migration.
_LEGACY_OWNER = 1


def upgrade() -> None:
    # -- F5: snag_source_messages.user_id -----------------------------------
    op.add_column(
        "snag_source_messages", sa.Column("user_id", sa.Integer(), nullable=True),
    )
    op.execute(f"UPDATE snag_source_messages SET user_id = {_LEGACY_OWNER} WHERE user_id IS NULL")
    op.alter_column("snag_source_messages", "user_id", nullable=False)

    op.create_foreign_key(
        "fk_snag_source_messages_user_id_users",
        "snag_source_messages", "users",
        ["user_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_snag_source_messages_user_id", "snag_source_messages", ["user_id"],
    )

    op.drop_constraint(
        "uq_snag_source_messages_message_ref", "snag_source_messages", type_="unique",
    )
    op.create_unique_constraint(
        "uq_snag_source_messages_user_ref",
        "snag_source_messages", ["user_id", "message_ref"],
    )

    op.add_column(
        "snag_source_messages",
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    # -- F4: install_codes.token_plaintext encrypted at rest -----------------
    # See module docstring — 24h/single-use window makes "null the legacy
    # value" the right call rather than migrating plaintext through Fernet
    # in-place.
    op.execute("UPDATE install_codes SET token_plaintext = NULL WHERE token_plaintext IS NOT NULL")
    op.alter_column(
        "install_codes", "token_plaintext",
        existing_type=sa.String(length=64), type_=sa.Text(), nullable=True,
    )


def downgrade() -> None:
    # IF EXISTS throughout — see user-memory `feedback_alembic_migration_safety`:
    # a partially-applied upgrade must still be reversible.

    # -- F4 ---------------------------------------------------------------
    # Any ciphertext currently in place would overflow String(64) — nulled
    # for the same reason as upgrade() (codes are 24h/single-use; nothing
    # of value survives a downgrade regardless).
    op.execute("UPDATE install_codes SET token_plaintext = NULL WHERE token_plaintext IS NOT NULL")
    op.alter_column(
        "install_codes", "token_plaintext",
        existing_type=sa.Text(), type_=sa.String(length=64), nullable=True,
    )

    # -- F5 -----------------------------------------------------------------
    op.execute("ALTER TABLE snag_source_messages DROP COLUMN IF EXISTS created_at")

    op.execute(
        "ALTER TABLE snag_source_messages "
        "DROP CONSTRAINT IF EXISTS uq_snag_source_messages_user_ref"
    )
    # Recreate the original global-uniqueness constraint. This can fail if
    # two users now hold rows with the same message_ref — an inherent tension
    # of reverting a scope-widening fix, same as vault_chunks_user_scope's
    # downgrade; not attempted defensively beyond IF EXISTS on the drop side.
    op.create_unique_constraint(
        "uq_snag_source_messages_message_ref", "snag_source_messages", ["message_ref"],
    )

    op.execute("DROP INDEX IF EXISTS ix_snag_source_messages_user_id")
    op.execute(
        "ALTER TABLE snag_source_messages "
        "DROP CONSTRAINT IF EXISTS fk_snag_source_messages_user_id_users"
    )
    op.execute("ALTER TABLE snag_source_messages DROP COLUMN IF EXISTS user_id")
