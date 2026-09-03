"""sync_cursors — generic per-(integration, user, key) cursor bookkeeping
(V4 chunk 3.2, app.plugin.sync_runtime.SyncCursor).

Opt-in table; nothing existing migrates onto it in this revision.
user_id is nullable (single-account integrations like weather/lastfm have
no owning user for their cursor); ON DELETE CASCADE since a cursor row is
disposable bookkeeping, not data worth preserving after its user is gone.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_cursors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("integration", sa.String(length=50), nullable=False),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_sync_cursors_integration", "sync_cursors", ["integration"])
    op.create_index("ix_sync_cursors_user_id", "sync_cursors", ["user_id"])
    op.create_unique_constraint(
        "uq_sync_cursors_integration_user_key",
        "sync_cursors",
        ["integration", "user_id", "key"],
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sync_cursors_user_id")
    op.execute("DROP INDEX IF EXISTS ix_sync_cursors_integration")
    op.execute("DROP TABLE IF EXISTS sync_cursors")
