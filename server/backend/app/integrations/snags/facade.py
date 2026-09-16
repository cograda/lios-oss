"""snags' facade — capability `snags.query` (R5, absence detection).

The only sanctioned way another integration reads the snag register:
`get_capability("snags.query").unanswered(session, weeks)`, having declared
the capability in its own manifest's `depends_on`. Nothing may import
`..models` directly from outside this package —
`tests/test_capability_boundaries.py` enforces that.

Deliberately narrow: one read method for one consumer
(`app.integrations.tasks.absence.unanswered_snags`). Grow it if a second
caller needs something else; don't pre-build a general query surface nobody
asked for.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

# Snags with no trade response yet. `wont-fix`/`closed`/etc all count as
# "answered" even if unhappily — the household made a decision, which is the
# opposite of silence. See `models.py::STATUSES`.
UNANSWERED_STATUSES = ("open", "reported")


class SnagsFacade:
    def unanswered(self, session: Session, *, cutoff: datetime) -> list[dict]:
        """Snags still `open`/`reported` whose reporting predates `cutoff`.

        `reported_at` is the snag's own report time; a snag added without one
        (a manual entry, say) falls back to `created_at` so it isn't invisible
        to this check forever. Returns plain dicts (uid/room/trade/reported_at)
        — no ORM objects cross the facade boundary.
        """
        from app.integrations.snags.models import Snag

        rows = (
            session.query(Snag)
            .filter(Snag.status.in_(UNANSWERED_STATUSES))
            .all()
        )
        out = []
        for s in rows:
            origin = s.reported_at or s.created_at
            if origin is None or origin > cutoff:
                continue
            out.append({
                "uid": s.uid,
                "title": s.title,
                "room": s.room,
                "trade": s.trade,
                "status": s.status,
                "origin": origin,
            })
        return out


FACADE = SnagsFacade()
