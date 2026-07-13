"""Apple Reminders integration — push-based sync from Mac agent."""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func as sqlfunc

from app.db import get_db
from app.integrations.base import BaseIntegration
from app.integrations.apple_reminders.models import Reminder
from app.integrations.apple_reminders.tools import get_mcp_tools

logger = logging.getLogger(__name__)


class AppleRemindersIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "apple_reminders"

    @property
    def display_name(self) -> str:
        return "Apple Reminders"

    def sync(self) -> None:
        """Sync is push-based — the Mac agent POSTs to /api/reminders/sync."""
        logger.info("Apple Reminders sync is push-based (Mac agent → server)")

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return reminder summary for the dashboard."""
        db = get_db()
        with db.session() as session:
            total = session.query(Reminder).filter(Reminder.completed == False).count()  # noqa: E712
            overdue = (
                session.query(Reminder)
                .filter(
                    Reminder.completed == False,  # noqa: E712
                    Reminder.due_date != None,  # noqa: E711
                    Reminder.due_date < datetime.now(timezone.utc),
                )
                .count()
            )

            by_list = (
                session.query(Reminder.list_name, sqlfunc.count(Reminder.id))
                .filter(Reminder.completed == False)  # noqa: E712
                .group_by(Reminder.list_name)
                .all()
            )

            return {
                "total_incomplete": total,
                "overdue": overdue,
                "by_list": {name: count for name, count in by_list},
            }

    def sync_schedule(self) -> str | None:
        # No server-side schedule — Mac agent pushes on its own timer
        return None

    def is_configured(self) -> bool:
        # Always "configured" — the Mac agent handles auth
        return True
