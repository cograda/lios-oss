"""Task and project ledger.

The database is the source of truth and `vault/Task Backlog.md` becomes a
generated one-way view of it. The file already worked this way in miniature —
its own header calls Focus and This Week *"query lenses (subsets of the same
list), not separate copies"*, executed by the Obsidian Tasks plugin. This
moves the source from a markdown list to tables and the lenses from that
plugin to SQL.

⚠️ **The sync is one-way, permanently.** Two-way sync is forbidden by the
`reference_reminders_write_channel` scar: queued is not applied. One-way
projection is the only pattern here that has never produced a split-brain.

Schema and rationale: `models.py`, and the design of record at
`vault/Projects/lios/Plans/tasks-and-projects-2026-08.md`.
"""

from typing import Any

from app.plugin.bases import CapabilityService


class TasksIntegration(CapabilityService):
    """No external system is polled: tasks are captured by hand, by the
    inbox, or by a sweep. `sync()` is inherited as a no-op and the manifest's
    `schedule=None` already means the scheduler never calls it."""

    @property
    def name(self) -> str:
        return "tasks"

    @property
    def display_name(self) -> str:
        return "Tasks"

    def mcp_tools(self) -> list[dict[str, Any]]:
        from app.integrations.tasks import tools as task_tools

        return task_tools.mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from sqlalchemy import func as sa_func

        from app.db import get_db
        from app.integrations.tasks.models import Task

        db = get_db()
        with db.session() as session:
            by_status = dict(
                session.query(Task.status, sa_func.count(Task.id))
                .group_by(Task.status).all()
            )
        return {"by_status": by_status}
