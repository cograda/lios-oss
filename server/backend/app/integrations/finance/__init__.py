"""Finance integration — personal banking data (AIB, Revolut).

Data enters via CSV import, not API sync. Provides MCP tools for querying
transactions, categories, trends, and subscriptions.
"""

import logging
from typing import Any

from app.db import get_db
from app.integrations.base import BaseIntegration
from app.integrations.finance.services import get_dashboard_stats, get_category_breakdown, resolve_period
from app.integrations.finance.tools import get_mcp_tools

logger = logging.getLogger(__name__)


class FinanceIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "finance"

    @property
    def display_name(self) -> str:
        return "Finance"

    def sync(self) -> None:
        """Finance data is imported via CSV — no periodic sync needed."""
        logger.debug("Finance sync is a no-op (data enters via CSV import)")

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return current month stats + category breakdown for dashboard."""
        db = get_db()
        with db.session() as session:
            start, end, prev_start, prev_end = resolve_period("this_month")
            stats = get_dashboard_stats(
                session,
                start_date=start,
                end_date=end,
                prev_start=prev_start,
                prev_end=prev_end,
            )
            categories = get_category_breakdown(session, start_date=start, end_date=end)
            stats["top_categories"] = categories[:5]
            return stats

    def sync_schedule(self) -> str | None:
        return None  # Manual CSV import only

    def is_configured(self) -> bool:
        return True  # No external API keys needed
