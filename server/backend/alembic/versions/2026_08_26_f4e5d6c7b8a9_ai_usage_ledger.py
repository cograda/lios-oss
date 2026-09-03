"""ai usage ledger — one row per AI call, cloud or local.

Kernel-owned (see app/models/ai_usage.py) — lios workstream W2, chunk 1
("AI Broker — Role Registry and Usage Ledger", build order step 1). Written
only through app.services.ai_ledger, which is fire-and-forget and buffered
so a slow disk or a locked table never adds latency to a spoken answer.

Two nullable columns carry the whole design:

  `cost_usd` — NULL means "unknown", never "free". A local call is recorded
  with cost_usd = 0.0 explicitly; NULL is reserved for providers with no
  per-call price (a flat subscription). Conflating the two would quietly
  understate spend and make a cloud->local swap look like the workload
  disappearing.

  `input_rate`/`output_rate` — the $/1M rate used AT CALL TIME, not derived
  from cost_usd after the fact. coglib.llm.MODELS is hand-maintained and can
  go stale; storing the rate alongside the cost means a later correction
  doesn't silently rewrite history.

`role` is nullable and unused until the registry chunk (build order step 2)
fills it in.

Revision ID: f4e5d6c7b8a9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4e5d6c7b8a9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("caller", sa.String(length=128), nullable=False),
        sa.Column("units_in", sa.Integer(), server_default="0", nullable=False),
        sa.Column("units_out", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reasoning_units", sa.Integer(), server_default="0", nullable=False),
        sa.Column("seconds", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("input_rate", sa.Float(), nullable=True),
        sa.Column("output_rate", sa.Float(), nullable=True),
        sa.Column("ok", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_ai_usage_ts", "ai_usage", ["ts"])
    op.create_index("ix_ai_usage_caller_ts", "ai_usage", ["caller", "ts"])


def downgrade() -> None:
    # DROP ... IF EXISTS throughout: a partially-applied upgrade otherwise
    # leaves a downgrade that cannot run, which is the worst moment to
    # discover it (same rationale as the algo-harness migration).
    op.execute("DROP INDEX IF EXISTS ix_ai_usage_caller_ts")
    op.execute("DROP INDEX IF EXISTS ix_ai_usage_ts")
    op.execute("DROP TABLE IF EXISTS ai_usage")
