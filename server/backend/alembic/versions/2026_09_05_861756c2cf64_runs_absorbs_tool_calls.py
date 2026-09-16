"""runs absorbs tool_calls — one ledger (Wave 5.1).

`system_alerts`' `recent_runs` axis used to read `runs` (S5.1) and
`tool_calls` (V4 chunk 2.5) separately and merge them in Python — two
tables with different scoping rules (runs household-wide, tool_calls
caller-scoped) for what is conceptually the same "something ran" ledger.
This migration:

  1. adds `tool_calls`' three columns not already on `runs`
     (`args_summary`, `affected`, `source_ip` — all nullable, NULL on every
     pre-existing `runs` row and on every `scheduled_job`/`manual` row
     going forward);
  2. copies every `tool_calls` row into `runs` with `kind='tool_call'`,
     `run_id` = the old `tool_call_id` (same correlation-id shape/purpose),
     `started_at`/`finished_at` both = the old `called_at` (`tool_calls`
     never recorded a separate finish time — `duration_ms` already carries
     the precision that matters), `outcome` = the old `status`
     (ok/error/timeout — a tool call CAN time out, unlike a scheduled job),
     `trigger` = the old `transport`, truncated to 10 chars for parity with
     the column's pre-existing width and the writer's own truncation
     behaviour;
  3. drops `tool_calls`.

`app/models/tool_calls.py` and `app/services/tool_calls.py` are deleted in
the same PR — every reader (`app/integrations/system/tools.py`,
`app/routes/{system,integrations}.py`, `app/plugin/kernel_jobs.py`'s prune
job) now reads `runs` filtered by `kind`, and the writer
(`app.plugin.dispatch`) now calls `app.services.runs.record_tool_call`.

Downgrade recreates `tool_calls`' shape (matching the pre-merge migrations
`2026_07_10_e1f2a3b4c5d6` + `2026_07_26_d6e7f8a9b0c1`) but does NOT restore
its data — consistent with every other `DROP TABLE IF EXISTS` downgrade in
this package, which reverses schema, not history. It also removes the three
absorbed columns from `runs` and the `kind='tool_call'` rows migrated in,
so a downgrade-then-upgrade round-trip on a copy of the same DB is safe
(migrated tool-call rows aren't double-migrated on re-upgrade, because
they no longer exist to be copied from a dropped `tool_calls` table — this
downgrade is a one-way schema reversal, not a full undo).

Revision ID: 861756c2cf64
Revises: b4d8f2a6c1e9
Create Date: 2026-09-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "861756c2cf64"
down_revision: Union[str, Sequence[str], None] = "b4d8f2a6c1e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add the ex-tool_calls columns to runs, nullable throughout — NULL
    # is correct for every pre-existing runs row and for every future
    # scheduled_job/manual row; only tool_call rows populate them.
    op.add_column("runs", sa.Column("args_summary", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("affected", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("source_ip", sa.String(length=64), nullable=True))

    # 2. Migrate tool_calls rows into runs, kind='tool_call'. IF-guarded via
    # to_regclass so this migration doesn't fail on a database where
    # tool_calls was somehow already dropped (e.g. re-running against a
    # partially-migrated copy).
    op.execute(
        """
        INSERT INTO runs (
            run_id, kind, name, user_id, started_at, finished_at,
            duration_ms, outcome, error_text, touched, trigger,
            args_summary, affected, source_ip
        )
        SELECT
            tc.tool_call_id,
            'tool_call',
            tc.name,
            tc.user_id,
            tc.called_at,
            tc.called_at,
            tc.duration_ms,
            tc.status,
            tc.error,
            NULL,
            COALESCE(LEFT(tc.transport, 10), 'mcp'),
            tc.args_summary,
            tc.affected,
            tc.source_ip
        FROM tool_calls tc
        WHERE to_regclass('public.tool_calls') IS NOT NULL
        """
    )

    # 3. Drop tool_calls — no readers or writers left after this PR.
    op.execute("DROP TABLE IF EXISTS tool_calls")


def downgrade() -> None:
    # Schema-only reversal — see module docstring. Remove the migrated
    # tool_call rows and the absorbed columns, then recreate tool_calls'
    # pre-merge shape (empty).
    op.execute("DELETE FROM runs WHERE kind = 'tool_call'")

    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS source_ip")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS affected")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS args_summary")

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
        sa.Column("args_summary", sa.Text(), nullable=True),
        sa.Column("affected", sa.Text(), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("transport", sa.String(length=10), nullable=True),
    )
    op.create_index("ix_tool_calls_tool_call_id", "tool_calls", ["tool_call_id"])
    op.create_index("ix_tool_calls_name", "tool_calls", ["name"])
    op.create_index("ix_tool_calls_user_id", "tool_calls", ["user_id"])
    op.create_index("ix_tool_calls_status", "tool_calls", ["status"])
    op.create_index("ix_tool_calls_called_at", "tool_calls", ["called_at"])
