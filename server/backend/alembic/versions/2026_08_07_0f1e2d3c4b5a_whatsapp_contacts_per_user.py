"""Scope whatsapp_contacts per user.

Contacts were deliberately global while only one Baileys bridge existed — the
jid → display-name graph was shared household metadata and nothing else wrote
to it. A second bridge (one Baileys session per phone number, so one per user)
makes that a leak: `whatsapp_contacts` is *who a person talks to*, and each
user's delivered `CLAUDE.md` already tells them their own WhatsApp is private.

The same jid legitimately appears for both users — a shared group, a mutual
friend — and can carry a different display name for each, so uniqueness moves
from `jid` to `(user_id, jid)` rather than being dropped.

Existing rows belong to user 1: they were written by the only bridge that has
ever run. `server_default="1"` makes that backfill implicit and also keeps an
older bridge image (which omits the column) inserting successfully through a
rolling deploy — the default is deliberately NOT dropped afterwards for that
reason, matching `whatsapp_messages.user_id`.

Revision ID: 0f1e2d3c4b5a
Revises: 8a0218302603
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa

revision = "0f1e2d3c4b5a"
down_revision = "8a0218302603"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF EXISTS / IF NOT EXISTS throughout: this migration has to be safe to
    # re-run against a database where a previous attempt half-applied.
    op.execute(
        "ALTER TABLE whatsapp_contacts "
        "ADD COLUMN IF NOT EXISTS user_id INTEGER NOT NULL DEFAULT 1"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_whatsapp_contacts_user_id "
        "ON whatsapp_contacts (user_id)"
    )

    # FK last, so the column and its backfill are already in place.
    op.execute(
        "ALTER TABLE whatsapp_contacts "
        "DROP CONSTRAINT IF EXISTS fk_whatsapp_contacts_user_id"
    )
    op.execute(
        "ALTER TABLE whatsapp_contacts "
        "ADD CONSTRAINT fk_whatsapp_contacts_user_id "
        "FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT"
    )

    # Swap uniqueness jid -> (user_id, jid). Add the new one first so the table
    # is never briefly un-deduplicated for concurrent bridge inserts.
    op.execute(
        "ALTER TABLE whatsapp_contacts "
        "ADD CONSTRAINT uq_wa_user_contact UNIQUE (user_id, jid)"
    )
    op.execute(
        "ALTER TABLE whatsapp_contacts "
        "DROP CONSTRAINT IF EXISTS whatsapp_contacts_jid_key"
    )


def downgrade() -> None:
    # Reverting to a global jid uniqueness only works if no second user has
    # written contacts yet; fail loudly rather than silently discarding rows.
    conn = op.get_bind()
    extra = conn.execute(
        sa.text("SELECT COUNT(*) FROM whatsapp_contacts WHERE user_id <> 1")
    ).scalar()
    if extra:
        raise RuntimeError(
            f"{extra} whatsapp_contacts rows belong to users other than 1; "
            "a global jid unique constraint would collide. Remove or reassign "
            "them before downgrading."
        )

    op.execute(
        "ALTER TABLE whatsapp_contacts DROP CONSTRAINT IF EXISTS uq_wa_user_contact"
    )
    op.execute(
        "ALTER TABLE whatsapp_contacts "
        "ADD CONSTRAINT whatsapp_contacts_jid_key UNIQUE (jid)"
    )
    op.execute(
        "ALTER TABLE whatsapp_contacts "
        "DROP CONSTRAINT IF EXISTS fk_whatsapp_contacts_user_id"
    )
    op.execute("DROP INDEX IF EXISTS ix_whatsapp_contacts_user_id")
    op.execute("ALTER TABLE whatsapp_contacts DROP COLUMN IF EXISTS user_id")
