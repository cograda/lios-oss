"""notification_sends.user_id — F7 (vault/Projects/lios/Plans/hardening-2026-08.md).

`notification_sends` is a shared, no-`UserOwnedMixin` table — most of its rows
genuinely are household infrastructure alerts (a sync failing, a daemon gone
silent) with no single owner. But the sweep also writes at least one kind of
row that IS user-attributable: the `apple_health` data-coverage-gap issue
(`system/tools.py`'s axis 2b) embeds a specific `user_id` in its body text
("data gap for user 2: ..."), and `notify_recent` returned every row to any
caller unfiltered — a real cross-user body-text leak.

This column is a plain nullable FK, deliberately NOT `UserOwnedMixin` (which
is NOT NULL): most rows have no single owner and must stay visible
household-wide, so NULL is the normal case, not a migration artifact.
`ON DELETE SET NULL` rather than RESTRICT for the same reason — deleting a
user should not be blocked by, or destroy, infrastructure alert history that
happens to have been attributed to them.

`sweep.py::collect()` now parses the `"data gap for user N"` issue shape and
stamps the resulting `AlertItem.target_user_id`; `reconcile()` writes it onto
the ledger row. `notifications/tools.py::handle_recent` scopes reads:
NULL rows (household-shared) plus, if a user is bound, that user's own rows;
unbound (the notifications sweep's own read paths, if any, and the dashboard)
sees the same NULL-only default — per `auth/context.py`'s documented stance
that unbound means "household-shared only," never "everyone's private data."

Existing rows all backfill to NULL (unattributable retroactively — the body
text isn't parsed on migrate, only on future sweeps).

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-08-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8b9c0d1e2f3a"
down_revision: Union[str, Sequence[str], None] = "7a8b9c0d1e2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notification_sends", sa.Column("user_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_notification_sends_user_id_users",
        "notification_sends", "users",
        ["user_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_notification_sends_user_id", "notification_sends", ["user_id"],
    )


def downgrade() -> None:
    # IF EXISTS throughout — see user-memory `feedback_alembic_migration_safety`:
    # a partially-applied upgrade must still be reversible.
    op.execute("DROP INDEX IF EXISTS ix_notification_sends_user_id")
    op.execute(
        "ALTER TABLE notification_sends "
        "DROP CONSTRAINT IF EXISTS fk_notification_sends_user_id_users"
    )
    op.execute("ALTER TABLE notification_sends DROP COLUMN IF EXISTS user_id")
