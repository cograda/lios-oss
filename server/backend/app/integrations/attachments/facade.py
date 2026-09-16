"""attachments' declared facade — capability `attachments.query`.

First consumer is the daily brief, which surfaces documents shared over
WhatsApp/email that haven't been ingested yet. Timeliness is the point:
WhatsApp's CDN purges sender bytes after ~30 days, so an un-ingested
attachment has an expiry date and the brief flags the old ones.

Every tool in this package is DSL-built, so there is no `handle_*`
function to import — see `app.tools.helpers.handler_for`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.attachments.tools import mcp_tools
from app.tools.helpers import handler_for

# NB this package spells it `mcp_tools`, not `get_mcp_tools` like the others.
_PENDING_HANDLER = handler_for(mcp_tools(), "attachments_pending")


class AttachmentsFacade:
    def pending(self, session: Session, arguments: dict[str, Any]) -> str:
        """Un-ingested attachments, newest first."""
        return _PENDING_HANDLER(session, arguments)


FACADE = AttachmentsFacade()
