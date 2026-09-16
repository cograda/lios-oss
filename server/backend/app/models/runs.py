"""`runs` — one structured row per scheduled job / tool call / manual
invocation (S5.1, `vault/Projects/lios/Backlog.md`; Wave 5.1: `tool_calls`
absorbed 2026-09-05).

Kernel-owned, not per-integration: `app.services.runs.record_run` is called
from `app.scheduler` for every registered job (see that module's generic
wrap in `setup_scheduler()`), so the writer has to be importable from
anywhere a job lives without creating a dependency cycle — `system` is a
leaf and may not import `tasks`/`household`/etc, so this table and its
writer live in `app/models/` + `app/services/`, never inside an integration
(see `app/integrations/tasks/absence.py`'s docstring for the cycle this
sidesteps).

**ONE ledger as of Wave 5.1.** This table used to cover only scheduled
jobs, with a separate `tool_calls` table for per-tool-call dispatch audit
(MCP + HTTP) — `system_alerts`' `recent_runs` axis read both and merged
them in Python. `tool_calls` had no readers or writers left after the
merge and was dropped in the same migration
(`alembic/versions/2026_09_05_<rev>_runs_absorbs_tool_calls.py`, which also
migrates its rows in with `kind="tool_call"`). A tool call and a scheduled
job are now rows of the same shape, distinguished by `kind`:

  - `scheduled_job` — an APScheduler-wrapped job (`app.scheduler.
    _wrap_scheduled_job`). Household-wide, `user_id` NULL.
  - `tool_call` — one MCP/HTTP tool dispatch (`app.plugin.dispatch.
    dispatch_tool`, via `app.services.runs.record_tool_call`). Caller-scoped,
    `user_id` set to the caller.
  - `manual` — an ad-hoc `record_run()` invocation (CLI, a script).

`args_summary`/`affected`/`source_ip` are `tool_call`-only columns (ex-
`ToolCall` columns, carried over verbatim) — NULL on every `scheduled_job`/
`manual` row. `outcome` is `ok`/`error`/`skipped` for `scheduled_job`/
`manual` rows and `ok`/`error`/`timeout` for `tool_call` rows (a tool call
can time out; a scheduled job's wrapper never produces that outcome) —
`system_runs`'/`recent_runs`' `outcome` filter therefore has a wider
vocabulary than either kind uses alone. `trigger` is `schedule`/`manual`
for non-tool rows and the dispatch transport (`mcp`/`http`, truncated to
10 chars — see `record_tool_call`) for `tool_call` rows.

Nullable `user_id`, same rationale as `AuthEvent`
(`app/privacy.py::NULLABLE_OR_ADMIN_USER_ID`): most rows are a
household-wide scheduled job with no bound caller, but `tool_call` rows DO
carry a real per-caller `user_id` — this is an admin/ops audit trail, not
per-user application data, same shape `tool_calls` had. `ON DELETE SET
NULL` so a user row can be removed without cascading into the run ledger.

**Scoping rule, explicit because two kinds share one table and one
`user_id` column with different meanings:** `scheduled_job`/`manual` rows
are household-wide and must never be filtered by caller — a per-user
caller still sees every scheduled job. `tool_call` rows carry a real
caller identity and ARE scoped: a bound caller sees only their own tool
calls, an unbound/household caller (`scope_user_id=None`) sees every call.
See `app.services.runs.recent_activity`'s `scope_user_id` handling — the
single query it runs implements exactly this split (`kind != 'tool_call'
OR user_id == scope_user_id`), so the two views can never drift apart.

`touched` is free-form JSONB — small counts/paths a job reports about what
it did (e.g. `{"users_warmed": 2, "users_total": 2}` for the daily brief
pre-warm, `{"minted": [...], "skipped": [...]}` for the routines tick) — not
a schema, just enough for a human or `system_runs` caller to see what a run
actually touched without going to the logs. Always NULL for `tool_call`
rows (they have no equivalent free-form summary; `args_summary` is the
per-tool-call analogue).
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_started_at", "started_at"),
        Index("ix_runs_name_started_at", "name", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Short per-run correlation id (secrets.token_hex(4)) — lets an ops
    # person tie a log line to a row. For `tool_call` rows this is the same
    # `tool_call_id` already threaded through `app.plugin.dispatch`'s
    # completion log line and `ToolResult.tool_call_id` — the ex-ToolCall
    # correlation id, reused rather than duplicated.
    run_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # scheduled_job|tool_call|manual|script
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # Indexed via __table_args__ above (both alone and as (name, started_at))
    # — no bare index=True here, which would mint a second same-named index.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ok|error|skipped for scheduled_job/manual, ok|error|timeout for
    # tool_call. Set on enter to a placeholder ("error", so a process that
    # dies mid-run without ever reaching __exit__ still reads as failed
    # rather than a silent "ok") and overwritten on exit — record_tool_call
    # instead writes the final outcome in one shot, since dispatch_tool
    # already knows it by the time it calls in.
    outcome: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form small JSON — see module docstring. Never large: this is a
    # summary, not an export. Always NULL for tool_call rows.
    touched: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)  # schedule|manual|mcp|http

    # --- ex-`ToolCall` columns (Wave 5.1) — populated for kind="tool_call"
    # only, always NULL otherwise. Carried over verbatim from `tool_calls`
    # (V4 chunk 2.5): see `app.services.redaction.scrub_args` for
    # `args_summary`, `app.plugin.dispatch.set_affected` for `affected`.
    args_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded list of entity refs the call touched, e.g.
    # '["snag:SNAG-0042"]'. Null for tools that don't set it (most tools).
    affected: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
