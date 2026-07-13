"""tool_calls — per-call audit trail for MCP/HTTP tool dispatch

One row per tool invocation (name, user, duration, status, error), written
best-effort by app.services.tool_calls.record_tool_call from both
app/mcp/server.py::call_tool and app/api/v1.py::call_tool. Feeds
system_alerts (repeated-failure + p95-latency checks). Pruned daily
alongside client_logs (see app/scheduler.py::run_prune_tool_calls).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tool_call_id", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "called_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_tool_calls_tool_call_id", "tool_calls", ["tool_call_id"])
    op.create_index("ix_tool_calls_name", "tool_calls", ["name"])
    op.create_index("ix_tool_calls_user_id", "tool_calls", ["user_id"])
    op.create_index("ix_tool_calls_status", "tool_calls", ["status"])
    op.create_index("ix_tool_calls_called_at", "tool_calls", ["called_at"])


def downgrade() -> None:
    for idx in (
        "ix_tool_calls_called_at",
        "ix_tool_calls_status",
        "ix_tool_calls_user_id",
        "ix_tool_calls_name",
        "ix_tool_calls_tool_call_id",
    ):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS tool_calls")
