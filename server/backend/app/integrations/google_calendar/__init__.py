"""Google Calendar integration — multi-account sync (read) + event creation
(write). Canonical `BidirectionalIntegration` conversion — V4 chunk 4.1.

This is the file future integrations should copy the shape of:

  - `sync()` is entirely inherited from `BidirectionalIntegration` (=
    `SourceIntegration` + outbound-action interface) — this class supplies
    only `accounts()`, `pull()`, `store()`, tool wiring, and dashboard data.
    No hand-rolled fan-out loop, no `sync_schedule()` override (the
    manifest's `schedule` field is the single source of truth the kernel
    scheduler reads directly — see `app/scheduler.py`, V4 chunk 3.1).
  - `client.py` keeps the Google API wrapper and delegates HTTP-error
    classification to `app.plugin.sync_runtime.classify_exc` — no local
    copy of a status->error-type mapping.
  - `tools.py` returns DSL-built tool dicts (`CustomTool`), not raw dicts —
    every tool still carries its own inline MCP `annotations` (unchanged
    from before this conversion; see `tests/test_plugin_discovery.py`'s
    frozen `OLD_TOOL_ANNOTATIONS` for the exact values this must keep
    matching).
"""

from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.google_calendar.sync import pull_calendar_events, store_calendar_events
from app.integrations.google_calendar.tools import get_mcp_tools, query_events
from app.models.tokens import OAuthToken
from app.plugin.bases import BidirectionalIntegration, PullResult


class GoogleCalendarIntegration(BidirectionalIntegration):
    """Multi-account Google Calendar sync (read) plus event creation (write).

    `accounts()` resolves every stored Google OAuth token (one per family
    member's connected account) — `SourceIntegration.sync()` fans out over
    these, calling `pull()`/`store()` once per account and aggregating
    success/failure exactly as the old hand-rolled `sync()` did (see
    `app.plugin.sync_runtime.fan_out`'s docstring for the precise
    all-succeed/all-fail/mixed semantics this preserves).
    """

    @property
    def name(self) -> str:
        return "google_calendar"

    @property
    def display_name(self) -> str:
        return "Google Calendar"

    def accounts(self, session: Session) -> list[OAuthToken]:
        return session.query(OAuthToken).filter_by(provider="google").all()

    def account_user_id(self, account: OAuthToken) -> int | None:
        return account.user_id

    def account_label(self, account: OAuthToken) -> str:
        return account.account_email

    def pull(self, account: OAuthToken, session: Session, cursor: str | None) -> PullResult:
        return pull_calendar_events(account.account_email, session, user_id=account.user_id)

    def store(self, session: Session, records: list[dict]) -> int:
        return store_calendar_events(session, records)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return this week's calendar summary for the dashboard."""
        from app.db import get_db

        db = get_db()
        with db.session() as session:
            events = query_events(session, days=7)

            # Group by day
            by_day: dict[str, list] = {}
            for e in events:
                day = e["start"][:10]  # YYYY-MM-DD
                by_day.setdefault(day, []).append(e)

            # Count connected accounts
            account_count = (
                session.query(OAuthToken)
                .filter_by(provider="google")
                .count()
            )

            # V4 chunk 5.1 — generic dashboard envelope, additive alongside
            # the legacy keys above (frontend is migrating to `panels`; no
            # other consumer reads the legacy shape for this integration).
            from app.services.dashboard_panels import stat_panel, table_panel

            rows = [
                [
                    day,
                    e["start"],
                    e.get("summary") or "Busy",
                    e.get("calendar"),
                    e.get("location") or "",
                ]
                for day, evs in sorted(by_day.items())
                for e in evs
            ]

            return {
                "connected_accounts": account_count,
                "events_this_week": len(events),
                "by_day": by_day,
                "next_event": events[0] if events else None,
                "panels": [
                    stat_panel("Connected accounts", account_count),
                    stat_panel("Events this week", len(events)),
                    table_panel(
                        "This week",
                        ["Day", "Start", "Event", "Calendar", "Location"],
                        rows,
                    ),
                ],
            }

    def is_configured(self) -> bool:
        return bool(settings.google_client_id and settings.google_client_secret)
