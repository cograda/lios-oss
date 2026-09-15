"""google_calendar's declared facade — capability `calendar.query` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.google_calendar`. Currently one consumer: `system`'s
morning-briefing/week-ahead composites (via `app.plugin.capabilities.get_capability`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.google_calendar.tools import handle_list_events, handle_today


class GoogleCalendarFacade:
    def today(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_today(session, arguments)

    def list_events(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_list_events(session, arguments)


FACADE = GoogleCalendarFacade()
