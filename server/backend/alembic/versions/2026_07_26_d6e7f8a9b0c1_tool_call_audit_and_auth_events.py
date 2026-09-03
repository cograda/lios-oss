"""tool_calls audit columns + auth_events table (V4 chunk 2.5)

Adds `args_summary` (redacted, size-capped JSON), `affected` (nullable JSON
list of entity refs), `source_ip`, `transport` to `tool_calls` — all
nullable, since existing rows predate this chunk and there's no way to
backfill them retroactively.

Creates `auth_events` (ts, outcome, token_last4, source_ip, transport,
user_id nullable) — one row per 401 (either transport) and per explicit
token issue/revoke.

See app/models/tool_calls.py, app/models/auth_events.py,
app/services/redaction.py, app/services/auth_events.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("args_summary", sa.Text(), nullable=True))
    op.add_column("tool_calls", sa.Column("affected", sa.Text(), nullable=True))
    op.add_column("tool_calls", sa.Column("source_ip", sa.String(length=64), nullable=True))
    op.add_column("tool_calls", sa.Column("transport", sa.String(length=10), nullable=True))

    op.create_table(
        "auth_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "ts", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("token_last4", sa.String(length=4), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("transport", sa.String(length=10), nullable=True),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.create_index("ix_auth_events_ts", "auth_events", ["ts"])
    op.create_index("ix_auth_events_outcome", "auth_events", ["outcome"])
    op.create_index("ix_auth_events_user_id", "auth_events", ["user_id"])


def downgrade() -> None:
    for idx in ("ix_auth_events_user_id", "ix_auth_events_outcome", "ix_auth_events_ts"):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS auth_events")

    op.execute("ALTER TABLE tool_calls DROP COLUMN IF EXISTS transport")
    op.execute("ALTER TABLE tool_calls DROP COLUMN IF EXISTS source_ip")
    op.execute("ALTER TABLE tool_calls DROP COLUMN IF EXISTS affected")
    op.execute("ALTER TABLE tool_calls DROP COLUMN IF EXISTS args_summary")
