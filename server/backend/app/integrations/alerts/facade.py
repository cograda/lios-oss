"""alerts' facade — the only cross-package import surface for this
integration (V4 chunk 4.2). `system` consumes `alerts.query` to build the
`system_alert_log` MCP tool and the daily brief's monitoring sub-block,
rather than importing `app.integrations.alerts.service` directly.

`events_since` takes `(session, arguments: dict)` and returns a JSON
string, matching every other facade method the daily brief's `Source`
fan-out calls (`calendar.query.today`, `weather.query.current`, ...) —
see `system/brief.py::_fetch_one`, which always calls
`method(session, dict(source.args))`.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.alerts import service
from app.tools.base import parse_iso_date


class AlertsFacade:
    def events_since(self, session: Session, arguments: dict[str, Any]) -> str:
        since_arg = arguments.get("since")
        since = parse_iso_date(since_arg) if since_arg else None
        if since is None:
            return json.dumps({"error": f"invalid or missing 'since': {since_arg!r}"})
        limit = int(arguments.get("limit", 200))
        payload = service.alert_events_since(session, since, limit=limit)
        return json.dumps(payload, default=str)


FACADE = AlertsFacade()
