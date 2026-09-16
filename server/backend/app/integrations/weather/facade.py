"""weather's declared facade — capability `weather.query` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.weather`. Currently one consumer: `system`'s
morning-briefing/week-ahead composites.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.weather.tools import handle_current, handle_forecast


class WeatherFacade:
    def current(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_current(session, arguments)

    def forecast(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_forecast(session, arguments)


FACADE = WeatherFacade()
