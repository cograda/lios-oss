"""obsidian's declared facade — capability `vault.query` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.obsidian`. Currently one consumer: `system`'s
search-everything composite.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.obsidian.tools import handle_search


class ObsidianFacade:
    def search(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_search(session, arguments)


FACADE = ObsidianFacade()
