"""coffee's declared facade — capability `coffee.query`.

Originally added for sam-rollout D1 so `app.mcp.instructions` (kernel
code) could check whether a user has any brew rows without importing
`app.integrations.coffee.models` directly, per the kernel/integration
import boundary (`tests/test_kernel_import_guard.py`).

Promoted to a declared capability for the daily brief, which needs the
current bag(s) and recent brews as two of its ~23 composed sources.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.integrations.coffee.models import CoffeeBrew
from app.integrations.coffee.tools import get_mcp_tools, handle_current
from app.tools.helpers import handler_for

# coffee_recent_brews is DSL-built (ListTool) — see `handler_for`.
_RECENT_BREWS_HANDLER = handler_for(get_mcp_tools(), "coffee_recent_brews")


class CoffeeFacade:
    def current(self, session: Session, arguments: dict[str, Any]) -> str:
        """Bags marked `status=current` — what the user is drinking."""
        return handle_current(session, arguments)

    def recent_brews(self, session: Session, arguments: dict[str, Any]) -> str:
        return _RECENT_BREWS_HANDLER(session, arguments)

    def has_data(self, session: Session, user_id: int) -> bool:
        """Cheap presence check — used by `app.mcp.instructions` to decide
        whether to offer this integration in a user's personalized render
        (sam-rollout D1)."""
        count = (
            session.query(func.count(CoffeeBrew.id))
            .filter(CoffeeBrew.user_id == user_id)
            .scalar()
        )
        return bool(count)


FACADE = CoffeeFacade()
