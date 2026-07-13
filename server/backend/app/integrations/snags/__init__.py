"""Snag register integration.

The snags database is the source of truth for house defects: each snag has an
immutable UID (SNAG-0042), a responsible trade, a severity, and a tracked
lifecycle (open → reported → accepted/disputed → fixed → verified → closed).
Evidence photos link into the media store; the vault note
(Household/Renovation/Snags.md) is a generated one-way projection.

Capture is idempotent over WhatsApp 'Snag - …' messages — the /triage and
/refresh skills call snag_capture instead of hand-editing the Project Board.
"""

from typing import Any

from app.integrations.base import BaseIntegration
from app.integrations.snags import tools as snag_tools


class SnagsIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "snags"

    @property
    def display_name(self) -> str:
        return "Snag Register"

    def sync(self) -> None:
        """No scheduled sync — capture is user-gated (snag_capture) so snags
        aren't silently created from mid-conversation messages."""
        return None

    def mcp_tools(self) -> list[dict[str, Any]]:
        return snag_tools.mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from sqlalchemy import func as sa_func
        from app.db import get_db
        from app.integrations.snags.models import Snag

        db = get_db()
        with db.session() as session:
            by_status = dict(
                session.query(Snag.status, sa_func.count(Snag.id))
                .group_by(Snag.status).all()
            )
            by_trade = dict(
                session.query(Snag.trade, sa_func.count(Snag.id))
                .group_by(Snag.trade).all()
            )
        return {"by_status": by_status, "by_trade": by_trade}

    def sync_schedule(self) -> str | None:
        return None

    def is_configured(self) -> bool:
        return True
