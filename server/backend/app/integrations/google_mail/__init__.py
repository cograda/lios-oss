"""Gmail integration — read-only access to Alex and Sam's inboxes.

`SourceIntegration` conversion (V4 chunk 4.3, batch C). `tools.py` was
already DSL-native before this chunk (mix of `CustomTool`/`ListTool`/
`SearchTool`/`SemanticSearchTool`/`StatsTool`, zero raw `inputSchema`
dicts), so this batch is purely the class/sync conversion:
`accounts()` resolves the Google OAuth tokens scoped to gmail (same
account-selection logic the old hand-rolled `sync()` used), and
`pull()`/`store()` are one-line adapters onto `sync.pull_mail`/
`sync.store_mail`. `sync()` itself is fully inherited from
`SourceIntegration` — no more hand-rolled `fan_out` call here.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.integrations.google_mail.models import MailMessage
from app.integrations.google_mail.sync import pull_mail, store_mail
from app.integrations.google_mail.tools import get_mcp_tools
from app.models.tokens import OAuthToken
from app.plugin.bases import PullResult, SourceIntegration

logger = logging.getLogger(__name__)


class GoogleMailIntegration(SourceIntegration):
    @property
    def name(self) -> str:
        return "google_mail"

    @property
    def display_name(self) -> str:
        return "Gmail"

    def accounts(self, session: Session) -> list[OAuthToken]:
        """Only Google OAuth tokens that carry gmail scopes — same filter
        the old hand-rolled `sync()` applied before fanning out."""
        tokens = session.query(OAuthToken).filter_by(provider="google").all()
        gmail_accounts = [t for t in tokens if t.scopes and "gmail" in t.scopes]
        if not gmail_accounts:
            logger.info("No Gmail accounts configured — skipping mail sync")
        return gmail_accounts

    def account_user_id(self, account: OAuthToken) -> int | None:
        return account.user_id

    def account_label(self, account: OAuthToken) -> str:
        return account.account_email

    def pull(self, account: OAuthToken, session: Session, cursor: str | None) -> PullResult:
        return pull_mail(account.account_email, session, user_id=account.user_id)

    def store(self, session: Session, records: list[dict]) -> int:
        return store_mail(session, records)

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
