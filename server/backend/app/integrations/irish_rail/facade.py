"""irish_rail's declared facade — capability `rail.query`.

irish_rail had no facade at all: it has no models and no sync (live API,
no caching), and nothing outside the package had ever needed to call it.
The daily brief is the first consumer — the Transport section wants the
next few departures for the user's configured station and direction.

Note this is one of the two genuinely *live* sources in the brief (the
other being Home Assistant's home status). Departure boards change minute
to minute, so the brief's cache must never serve this from a warm entry —
stale DARTs are worse than no DARTs.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.irish_rail.tools import handle_departures


class IrishRailFacade:
    def departures(self, session: Session, arguments: dict[str, Any]) -> str:
        """Departure board for a station.

        Unconfigured is not an error here: `handle_departures` returns a
        message naming the fix when no `station` argument is passed and no
        `rail_station_code` is configured, which the brief surfaces as-is.
        """
        return handle_departures(session, arguments)


FACADE = IrishRailFacade()
