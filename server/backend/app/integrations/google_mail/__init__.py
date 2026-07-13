"""Gmail integration — read-only access to Alex and Sam's inboxes."""

import logging
from typing import Any

from app.config import settings
from app.db import get_db
from app.errors import NeedsReauthError, PermanentError, TransientError
from app.integrations.base import BaseIntegration
from app.integrations.google_mail.models import MailMessage
from app.integrations.google_mail.sync import sync_mail
from app.integrations.google_mail.tools import get_mcp_tools
from app.models.tokens import OAuthToken

logger = logging.getLogger(__name__)


class GoogleMailIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "google_mail"

    @property
    def display_name(self) -> str:
        return "Gmail"

    def sync(self) -> None:
        """Sync all configured Gmail accounts."""
        db = get_db()
        with db.session() as session:
            tokens = (
                session.query(OAuthToken)
                .filter_by(provider="google")
                .all()
            )

            # Only sync accounts that have gmail scopes
            gmail_accounts = [
                t for t in tokens
                if t.scopes and "gmail" in t.scopes
            ]

            if not gmail_accounts:
                logger.info("No Gmail accounts configured — skipping mail sync")
                return

            total = 0
            successes = 0
            failures: list[str] = []
            failure_excs: list[Exception] = []
            for token in gmail_accounts:
                try:
                    count = sync_mail(
                        token.account_email, session, user_id=token.user_id,
                    )
                    total += count
                    successes += 1
                except Exception as e:
                    logger.exception(f"Failed to sync mail for {token.account_email}")
                    failures.append(f"{token.account_email}: {type(e).__name__}: {e}")
                    failure_excs.append(e)

            logger.info(
                f"Mail sync complete: {total} messages, "
                f"{successes}/{len(gmail_accounts)} accounts ok"
            )

            # If every account failed, surface that to the scheduler so SyncState
            # records "error" rather than silent "ok with zero messages". This is
            # how the OAuth-refresh-revoked bug hid for 10 days on 2026-04-28.
            # Raise PermanentError only if every account failed for a permanent
            # reason (dead token, disabled API, ...) — a mix, or any transient
            # failure, means a retry is still worth trying.
            if successes == 0 and gmail_accounts:
                message = (
                    f"All {len(gmail_accounts)} Gmail accounts failed: "
                    + " | ".join(failures)
                )
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
        """Return email summary for the dashboard."""
        db = get_db()
        with db.session() as session:
            total_unread = (
                session.query(MailMessage)
                .filter(MailMessage.is_read == False)  # noqa: E712
                .count()
            )

            # Unread by account
            from sqlalchemy import func
            by_account = (
                session.query(
                    MailMessage.account_email,
                    func.count(MailMessage.id),
                )
                .filter(MailMessage.is_read == False)  # noqa: E712
                .group_by(MailMessage.account_email)
                .all()
            )

            # Most recent message date
            latest = (
                session.query(MailMessage.date)
                .order_by(MailMessage.date.desc().nullslast())
                .first()
            )

            return {
                "total_unread": total_unread,
                "by_account": {email: count for email, count in by_account},
                "latest_message": latest[0].isoformat() if latest and latest[0] else None,
                "total_cached": session.query(MailMessage).count(),
            }

    def sync_schedule(self) -> str | None:
        return "*/15 * * * *"  # Every 15 minutes

    def is_configured(self) -> bool:
        """Configured if Google OAuth credentials are set and at least one account has gmail scopes."""
        if not settings.google_client_id or not settings.google_client_secret:
            return False

        db = get_db()
        with db.session() as session:
            gmail_account = (
                session.query(OAuthToken)
                .filter(
                    OAuthToken.provider == "google",
                    OAuthToken.scopes.contains("gmail"),
                )
                .first()
            )
            return gmail_account is not None
