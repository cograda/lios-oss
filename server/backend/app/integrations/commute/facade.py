"""commute's declared facade — capability `commute.query` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.commute`. Currently one consumer: `system`'s
morning-briefing composite, which only wants the *current* decision
(state/status_text/leave_in_min) when it's fresh — the "only include when
there's a fresh decision" staleness logic used to live inline in
`system/tools.py`; it now lives here, next to the model it reads.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.integrations.commute.models import CommuteDecision
from app.tools.helpers import age_seconds

#: How fresh a decision has to be to surface in the morning briefing.
FRESH_WINDOW_SECONDS = timedelta(minutes=10).total_seconds()


class CommuteFacade:
    def recent_decision(self, session: Session) -> dict | None:
        """Latest commute decision, if it's still fresh (within
        `FRESH_WINDOW_SECONDS`). Returns None otherwise — a stale weekend/
        evening decision shouldn't show up in an unrelated morning's
        briefing."""
        latest = (
            session.query(CommuteDecision)
            .order_by(CommuteDecision.decided_at.desc())
            .first()
        )
        if latest is None:
            return None
        age = age_seconds(latest.decided_at)
        if age is None or age >= FRESH_WINDOW_SECONDS:
            return None
        return {
            "state": latest.state,
            "status_text": latest.status_text,
            "leave_in_min": latest.leave_in_min,
        }


FACADE = CommuteFacade()
