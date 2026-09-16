"""Finance integration — personal banking data (AIB, Revolut).

Data enters via CSV import, not any external sync — a `CapabilityService`
(V4 chunk 4.2): no `pull()`/`store()`, no external system. `sync()`,
`is_configured()`, and `dashboard_data()`'s no-op defaults are all inherited
from `CapabilityService`/`BaseIntegration`; this class only wires
`mcp_tools()` and overrides `dashboard_data()` with the real dashboard
summary. `services.py` (the categorisation/analytics engine) is unchanged by
this conversion — it was never part of the old ABC surface.
"""

from typing import Any

from app.db import get_db
from app.integrations.finance.services import get_dashboard_stats, get_category_breakdown, resolve_period
from app.integrations.finance.tools import get_mcp_tools
from app.plugin.bases import CapabilityService


class FinanceIntegration(CapabilityService):
    @property
    def name(self) -> str:
        return "finance"

    @property
    def display_name(self) -> str:
        return "Finance"

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

            # V4 chunk 5.1 — generic dashboard envelope, additive alongside
            # the legacy keys above (finance_summary's MCP tool reads
            # get_dashboard_stats()'s shape directly, snapshot-pinned —
            # unaffected by this addition).
            from app.services.dashboard_panels import stat_panel, table_panel

            prev = stats.get("previous") or {}

            def _trend(current: float, previous: float | None) -> dict[str, Any] | None:
                if not previous:
                    return None
                pct = ((current - previous) / abs(previous)) * 100
                sign = "+" if pct >= 0 else ""
                return {"value": f"{sign}{pct:.0f}% vs last", "positive": pct >= 0}

            stats["panels"] = [
                stat_panel(
                    "Income", stats["total_income"],
                    trend=_trend(stats["total_income"], prev.get("total_income")),
                ),
                stat_panel(
                    "Expense", stats["total_expense"],
                    trend=_trend(stats["total_expense"], prev.get("total_expense")),
                ),
                stat_panel(
                    "Net", stats["net_savings"],
                    trend={
                        "value": "saving" if stats["net_savings"] >= 0 else "overspend",
                        "positive": stats["net_savings"] >= 0,
                    },
                ),
                table_panel(
                    "Top spending",
                    ["Category", "Amount"],
                    [[c["name"], c["value"]] for c in stats["top_categories"]],
                ),
            ]
            return stats

    # is_configured(): default (empty required set -> vacuously True; no
    # external API keys needed).
