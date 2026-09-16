"""lastfm's facade — capability `music.query` (originally V4 chunk 4.3e).

Was a bare facade with no declared capability: its only consumer was
`app/routes/integrations.py`'s manual `POST /integrations/lastfm/backfill`
admin endpoint, a fixed 1:1 dependency where a direct facade import is fine.

The daily brief is a second, non-1:1 consumer (recent rotation + weekly
stats feed the Pulse block's Listening lines), so the capability is now
declared properly and resolved via `get_capability("music.query")`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.integrations.lastfm import sync as _sync
from app.integrations.lastfm.models import Scrobble
from app.integrations.lastfm.tools import get_mcp_tools
from app.tools.helpers import handler_for

# Both DSL-built (ListTool / StatsTool) — see `handler_for`.
_TOOLS = get_mcp_tools()
_RECENT_HANDLER = handler_for(_TOOLS, "lastfm_recent")
_STATS_HANDLER = handler_for(_TOOLS, "lastfm_stats")

# `import sync as _sync` + `_sync.fn(...)`, not `from sync import fn` — see
# the "never `from module import fn`" hard rule (V4 index, "Hard rules
# learned in execution"): the latter would silently stop tracking a
# monkeypatched `app.integrations.lastfm.sync.backfill_scrobbles`.


class LastfmFacade:
    def recent(self, session: Session, arguments: dict[str, Any]) -> str:
        return _RECENT_HANDLER(session, arguments)

    def stats(self, session: Session, arguments: dict[str, Any]) -> str:
        return _STATS_HANDLER(session, arguments)

    def backfill(self, session: Session, *, resume: bool = True) -> int:
        return _sync.backfill_scrobbles(session, resume=resume)

    def has_data(self, session: Session, user_id: int) -> bool:
        """Cheap presence check — used by `app.mcp.instructions` to decide
        whether to offer this integration in a user's personalized render
        (sam-rollout D1)."""
        count = (
            session.query(func.count(Scrobble.id))
            .filter(Scrobble.user_id == user_id)
            .scalar()
        )
        return bool(count)


FACADE = LastfmFacade()
