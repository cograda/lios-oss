"""Apple Reminders integration — push-based sync from Mac agent.

`BidirectionalIntegration` conversion (V4 chunk 4.3, batch C). The read side
(`sync()`) was already push-based dead weight before this chunk — the Mac
daemon POSTs to `/api/v1/reminders/push` on its own timer, the manifest
declares no server-side schedule, and the old `sync()` body was just a log
line (same shape as `apple_health`'s pre-batch-B vestige) — so `accounts()` returns `[]`
(nothing to fan out over; `SourceIntegration.sync()`'s own "no accounts
configured — skipping sync" log line takes over that job) and `pull()`/
`store()` are never actually invoked, just present to satisfy the ABC.

The write side is the existing enqueue → SSE → client-daemon EventKit
dispatch path (`commands.py::dispatch_command`/`complete_command`), formalized
onto `execute_action()` below. `tools.py`'s `handle_add_reminder`/
`handle_complete_reminder` continue to call `dispatch_command` directly
(unchanged) — `execute_action()` is an additive, uniform entrypoint onto the
same function, not a rewire of the live tool handlers, so the wire protocol
the daemon sees (SSE event shape, `/api/v1/reminders/commands/{id}/done`
ack, queue semantics) is untouched.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.apple_reminders.commands import dispatch_command
from app.integrations.apple_reminders.models import Reminder
from app.integrations.apple_reminders.tools import get_mcp_tools
from app.plugin.bases import ActionResult, BidirectionalIntegration, PullResult

logger = logging.getLogger(__name__)


class AppleRemindersIntegration(BidirectionalIntegration):
    @property
    def name(self) -> str:
        return "apple_reminders"

    @property
    def display_name(self) -> str:
        return "Apple Reminders"

    def accounts(self, session: Session) -> list[Any]:
        """No polling accounts — reminder state arrives via the Mac daemon's
        own `/api/v1/reminders/push` push, not a server-side fetch. Returning
        `[]` makes the inherited `sync()` log-and-skip, matching the old
        hand-written `sync()`'s "push-based, nothing to do" behaviour."""
        return []

    def pull(self, account: Any, session: Session, cursor: str | None) -> PullResult:
        """Never called — `accounts()` always returns `[]`. Present only to
        satisfy `SourceIntegration`'s abstract contract."""
        raise NotImplementedError("apple_reminders has no polling accounts")

    def store(self, session: Session, records: list[Any]) -> int:
        """Never called — `accounts()` always returns `[]`. Present only to
        satisfy `SourceIntegration`'s abstract contract."""
        raise NotImplementedError("apple_reminders has no polling accounts")

    async def execute_action(self, action: dict) -> ActionResult:
        """Formalize the existing command-queue dispatch onto the typed-base
        interface. `action` is `{"user_id": int, "user_name": str,
        "action": "add" | "complete", "args": dict}` — the same shape
        `dispatch_command` already takes. Runs the (blocking) dispatch in a
        thread since this method is a coroutine but `dispatch_command` does
        blocking DB + SSE-future waits internally.
        """
        import asyncio

        db = get_db()

        def _dispatch() -> dict:
            with db.session() as session:
                return dispatch_command(
                    session,
                    user_id=action["user_id"],
                    user_name=action["user_name"],
                    action=action["action"],
                    args=action.get("args", {}),
                )

        result = await asyncio.to_thread(_dispatch)
        return ActionResult(
            ok=bool(result.get("ok")),
            detail=result.get("reason") or result.get("error"),
            data=result,
        )

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

            # V4 chunk 5.1 — generic dashboard envelope, additive alongside
            # the legacy keys above.
            from app.services.dashboard_panels import list_panel, stat_panel

            by_list_dict = {name: count for name, count in by_list}

            return {
                "total_incomplete": total,
                "overdue": overdue,
                "by_list": by_list_dict,
                "panels": [
                    stat_panel(
                        "Open", total,
                        trend={"value": "needs attention", "positive": False} if overdue else None,
                    ),
                    stat_panel(
                        "Overdue", overdue,
                        trend={"value": "needs attention", "positive": False} if overdue > 0 else None,
                    ),
                    list_panel(
                        "By list",
                        [{"label": name, "value": count} for name, count in by_list_dict.items()],
                    ),
                ],
            }

    # is_configured(): default (empty config_schema -> vacuously True; the
    # Mac agent handles its own EventKit auth, nothing for us to check here).
