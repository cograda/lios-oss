"""backfill whatsapp_messages.chat_name from whatsapp_contacts

`chat_name` has been hardcoded null in the bridge since forever
(`server/whatsapp-bridge/src/index.js::extractMessage` set it to a literal
`null` before the group/1:1 branches, and nothing ever filled it in). Fixed
at the code level in the same change that adds this migration — the bridge
now resolves a name via an in-memory cache populated from Baileys'
`contacts.update` / history-sync contacts events before writing each
message. That only helps messages written from here on; this migration
recovers the ~134k existing rows, which are fully recoverable because the
contact name was captured correctly the whole time in the sibling
`whatsapp_contacts` table via a separate event stream.

MANDATORY: joins on (chat_id, user_id) = (jid, user_id), never on chat_id/jid
alone. A WhatsApp `@lid` is scoped to the account that observed it, not
globally unique (see `reference_whatsapp_lid_scoping.md` / the
`whatsapp_self_chat_jids` config note in server/CLAUDE.md) — the same LID can
name a different conversation under a different comar user. comar is
multi-user (Alex and Sam each run their own bridge), so joining on jid
alone would cross-attribute one user's chat name onto another user's
messages wherever a LID collides. `whatsapp_contacts` carries a
`uq_wa_user_contact` unique constraint on `(user_id, jid)`, which is what
makes `(m.chat_id = c.jid AND m.user_id = c.user_id)` a safe (not just
correct) join — at most one contact row per (user_id, jid).

UPDATE only. Never deletes or touches rows that already have a chat_name, and
never touches rows whose jid has no known contact name.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE whatsapp_messages m "
            "SET chat_name = c.name "
            "FROM whatsapp_contacts c "
            "WHERE m.chat_id = c.jid "
            "  AND m.user_id = c.user_id "  # mandatory: @lid is account-scoped, not global
            "  AND m.chat_name IS NULL "
            "  AND c.name IS NOT NULL "
            # Groups are EXCLUDED. whatsapp_contacts.name for an @g.us jid is a
            # participant's name, not the group subject: the school parents group
            # 120363145771673138@g.us is stored as "Sam", who merely posted in
            # it. Backfilling that would replace null with a confident wrong
            # answer — worse than the bug, because null is visibly missing and
            # "Sam" is not. Group subjects come from Baileys group metadata,
            # which the bridge does not consume yet. See the whatsapp chat_name
            # item in Projects/lios/Backlog.md.
            "  AND c.is_group = false"
        )
    )


def downgrade() -> None:
    # This backfill is not reversible in the sense of restoring "the value
    # before the migration" — that value was NULL, and NULL is exactly what
    # every affected row had. Downgrading re-nulls every chat_name this
    # migration set, distinguishing "set by this migration" from "a name
    # written later by the fixed bridge" isn't possible from stored state
    # alone (both look identical: a non-null chat_name matching the current
    # contact name) — so on downgrade we go back to the same
    # under-populated state this migration started from, not to a distinct
    # prior value.
    op.execute(
        sa.text(
            "UPDATE whatsapp_messages m "
            "SET chat_name = NULL "
            "FROM whatsapp_contacts c "
            "WHERE m.chat_id = c.jid "
            "  AND m.user_id = c.user_id "
            "  AND m.chat_name = c.name"
        )
    )
