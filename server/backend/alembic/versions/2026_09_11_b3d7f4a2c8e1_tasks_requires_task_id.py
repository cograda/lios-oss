"""tasks.requires_task_id — a round (or any task) can name one prerequisite

Revision ID: b3d7f4a2c8e1
Revises: 6548357098fa
Create Date: 2026-09-11

C2 chunk proposal (`vault/Projects/Hall Panel/C2 — runs with prerequisites
(proposal, 2026-09-10).md`), agreed by Alex 2026-09-11, section (c). One
nullable, single-edge FK — not a graph (a general `blocked_by` dependency
graph was already tried as `taskgraph` and parked 2026-08-13: the
LLM-inferred edge direction inverted itself, and every household example
here is one thing waiting on one other thing).

Declared on the TASK row rather than the routine template, because a round
IS a `tasks` row (`routines.py`'s module docstring) and the real cases
("put the bins out" waits on "bring the bins in", not on every Wednesday
forever) are day-specific, not template-level. Nothing stops an ordinary
one-off `Task` from using it too (open question 5 in the proposal) — the
column lives on `Task`, not on a routine-only table, so it already covers
both without extra schema.

⚠️ Deferred, per the proposal's own allowance: the optional
`RoutineStep`-level default (`requires_step_ord`) that `mint_round` would
resolve into a concrete `requires_task_id` at mint time. Resolving "which
task represents THIS cycle's occurrence of the prerequisite routine" needs
its own cross-routine lookup that isn't a small addition on top of this
column, so it is left as a TODO (see `routines.py::mint_round` and issue
#156) rather than shipped half-designed. The instance-level field ships
alone; setting it today is a `routines_update`/`tasks_update` call each
cycle.

Both objects (constraint, index) are dropped IF EXISTS before being
created, per the repo's migration-safety convention (see the 2026-09-07
`tasks.kind`/`tasks.severity` migration for the crash-loop this protects
against).
"""

from alembic import op
import sqlalchemy as sa

revision = "b3d7f4a2c8e1"
down_revision = "6548357098fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("requires_task_id", sa.Integer(), nullable=True),
    )
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS fk_tasks_requires_task_id_tasks")
    op.create_foreign_key(
        "fk_tasks_requires_task_id_tasks",
        "tasks", "tasks",
        ["requires_task_id"], ["id"],
    )
    op.execute("DROP INDEX IF EXISTS ix_tasks_requires_task_id")
    op.create_index("ix_tasks_requires_task_id", "tasks", ["requires_task_id"])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_tasks_requires_task_id")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS fk_tasks_requires_task_id_tasks")
    op.drop_column("tasks", "requires_task_id")
