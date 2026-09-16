"""tasks.source vocabulary + tasks.confirmed_at — lios#224

Revision ID: e6b1f4a9c3d7
Revises: 403950e87a6f
Create Date: 2026-09-14

Two halves of the same gap, fixed together per the issue's decided design:

1. **`source` was free text.** A live query of the household ledger found 21
   distinct strings against a documented vocabulary of 5 — session debris
   like `kickoff-triage`, `found-2026-09-07`, `voice memo 2026-09-10`, and
   four `triage (...)` variants — and an unvalidated caller crashed
   `tasks_add` outright: `source="meeting:2026-09-13 Household Systems &
   Weekly Check-in"` (55+ chars) raised `psycopg2.errors.
   StringDataRightTruncation` against the `String(30)` column. The column
   stays `String(30)`, nullable, with no DB CHECK — `app.integrations.tasks.
   models.TASK_SOURCES` is the single source of truth, enforced in Python at
   every write site so the vocabulary can grow without a mid-deploy
   migration. This migration only normalises what is ALREADY in the column;
   it does not add a constraint.

   Mapping (session-debris patterns first, most specific to least, so a
   value never matches two rules): `kickoff-triage` or anything starting
   `triage` -> `kickoff`; starting `voice memo` -> `voice`; starting
   `found-` -> `sweep`; starting `meeting` -> `meeting` (a no-op for the
   exact value, and folds in any `meeting: ...` variant history left
   behind); NULL -> `manual`; anything still not in the allowed set after
   all of the above -> `legacy`. The original string is not separately
   preserved — the issue is explicit that these are session debris, not
   data worth an audit trail, and the mapping is recorded here rather than
   via a per-row history entry (this package's cheapest history mechanism,
   `TaskEvent`, is ORM-only and this is a raw-SQL migration).

2. **`confirmed_at`** — a nullable, tz-aware timestamp gating "an extraction
   pass suggested this" from "this is a live, active task" in CODE rather
   than by prompt convention. Backfilled from `created_at` for every
   existing row (both directions of the issue's `Task.source`/
   `Task.confirmed_at` decisions ship together): every row created before
   this migration was made through a human-initiated or system path, never
   an unreviewed LLM suggestion, so treating "when it was created" as "when
   it was confirmed" is correct for 100% of existing data. Both objects are
   dropped IF EXISTS before creation, per this repo's migration-safety
   convention (see the 2026-09-07 `tasks.kind`/`tasks.severity` migration for
   the crash-loop this protects against).
"""

from alembic import op
import sqlalchemy as sa

revision = "e6b1f4a9c3d7"
# Chained after alert_events (lios#230) — both migrations were written the
# same day from the same parent and relinked at merge to keep one head.
down_revision = "a6f1c4b7d8e5"
branch_labels = None
depends_on = None

# Mirrors app.integrations.tasks.models.TASK_SOURCES. Not imported — Alembic
# migrations run against whatever the model looked like at the time they
# were written, not against `HEAD`'s import graph.
_ALLOWED_SOURCES = (
    "manual", "meeting", "kickoff", "seed", "seed-whatsapp", "voice", "sweep",
    "apple_reminders", "routine", "split", "backlog_import", "someday_import",
    "delegated_import", "legacy",
)


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("DROP INDEX IF EXISTS ix_tasks_confirmed_at")
    op.create_index("ix_tasks_confirmed_at", "tasks", ["confirmed_at"])

    # Every pre-existing row was made through a human or system path, never
    # an unreviewed suggestion — see the module docstring. COALESCE because
    # ~239 imported rows carry created_at = NULL (unrecoverable, per
    # models.py); a literal `= created_at` would leave them unconfirmed and
    # dump them into the backlog's `## Suggested` section on deploy.
    op.execute("UPDATE tasks SET confirmed_at = COALESCE(created_at, now())")

    # ── source normalisation — order matters, most specific first ──────────
    op.execute("""
        UPDATE tasks SET source = 'kickoff'
        WHERE source = 'kickoff-triage' OR source LIKE 'triage%'
    """)
    op.execute("UPDATE tasks SET source = 'voice' WHERE source LIKE 'voice memo%'")
    op.execute("UPDATE tasks SET source = 'sweep' WHERE source LIKE 'found-%'")
    op.execute("UPDATE tasks SET source = 'meeting' WHERE source LIKE 'meeting%'")
    op.execute("UPDATE tasks SET source = 'manual' WHERE source IS NULL")
    allowed_sql = ", ".join(f"'{s}'" for s in _ALLOWED_SOURCES)
    op.execute(f"UPDATE tasks SET source = 'legacy' WHERE source NOT IN ({allowed_sql})")


def downgrade() -> None:
    # The source normalisation is not reversible (the original strings are
    # not preserved anywhere — see the module docstring); downgrade only
    # removes the new column.
    op.execute("DROP INDEX IF EXISTS ix_tasks_confirmed_at")
    op.drop_column("tasks", "confirmed_at")
