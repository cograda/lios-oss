"""Google Calendar integration — 4 accounts (Alex, Sam, Finn, Isla)."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.db import get_db
from app.errors import NeedsReauthError, PermanentError, TransientError
from app.integrations.base import BaseIntegration
from app.integrations.google_calendar.models import CalendarEvent
from app.integrations.google_calendar.sync import sync_calendar
from app.integrations.google_calendar.tools import get_mcp_tools, query_events
from app.models.tokens import OAuthToken

logger = logging.getLogger(__name__)

# Accounts to sync — add all family accounts here
CALENDAR_ACCOUNTS: list[str] = []  # Populated from DB (any google token with calendar scopes)


class GoogleCalendarIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "google_calendar"

    @property
    def display_name(self) -> str:
        return "Google Calendar"

    def sync(self) -> None:
        """Sync all configured Google Calendar accounts."""
        db = get_db()
        with db.session() as session:
            # Find all Google accounts with stored tokens
            tokens = (
                session.query(OAuthToken)
                .filter_by(provider="google")
                .all()
            )

            if not tokens:
                logger.info("No Google accounts configured — skipping calendar sync")
                return

            total = 0
            successes = 0
            failures: list[str] = []
            failure_excs: list[Exception] = []
            for token in tokens:
                try:
                    count = sync_calendar(
                        token.account_email, session, user_id=token.user_id,
                    )
                    total += count
                    successes += 1
                except Exception as e:
                    logger.exception(f"Failed to sync calendar for {token.account_email}")
                    failures.append(f"{token.account_email}: {type(e).__name__}: {e}")
                    failure_excs.append(e)

            logger.info(
                f"Calendar sync complete: {total} events, "
                f"{successes}/{len(tokens)} accounts ok"
            )

            # If every account failed, surface that to the scheduler so SyncState
            # records "error" rather than silent "ok with zero events". Raise
            # PermanentError only if every account failed for a permanent reason
            # (dead token, disabled API, ...) — a mix, or any transient failure,
            # means a retry is still worth trying.
            if successes == 0 and tokens:
                message = f"All {len(tokens)} calendar accounts failed: " + " | ".join(failures)
                last_exc = failure_excs[-1]
                # A single dead-token account is the common case — preserve
                # the specific NeedsReauthError so the scheduler's "needs
                # re-auth" messaging still applies rather than a generic one.
                if len(failure_excs) == 1 and isinstance(last_exc, NeedsReauthError):
                    raise last_exc
                if all(isinstance(e, PermanentError) for e in failure_excs):
                    raise PermanentError(message) from last_exc
                raise TransientError(message) from last_exc

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return this week's calendar summary for the dashboard."""
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

            return {
                "connected_accounts": account_count,
                "events_this_week": len(events),
                "by_day": by_day,
                "next_event": events[0] if events else None,
            }

    def sync_schedule(self) -> str | None:
        return "*/15 * * * *"  # Every 15 minutes

    def is_configured(self) -> bool:
        return bool(settings.google_client_id and settings.google_client_secret)
