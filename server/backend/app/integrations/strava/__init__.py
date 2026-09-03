"""Strava integration — historical and ongoing activity sync.

A `SourceIntegration`: poll Strava on a schedule, upsert what changed.
`sync()` itself is inherited — this class supplies only `accounts()`,
`pull()` and `store()`.

Connecting an athlete is a browser flow (`/api/strava/connect?user=<name>`),
not a config key, because Strava is OAuth2. Until someone completes it,
`accounts()` returns `[]` and `SourceIntegration.sync()` logs "no accounts
configured — skipping sync". That is the intended shape of an unconfigured
Strava: a clean no-op, not a scheduled error every 30 minutes.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.strava.sync import PROVIDER, pull_activities, store_activities
from app.integrations.strava.tools import get_mcp_tools
from app.models.tokens import OAuthToken
from app.plugin.bases import PullResult, SourceIntegration


class StravaIntegration(SourceIntegration):
    """Poll `GET /athlete/activities` per connected athlete."""

    # No SyncCursor bookkeeping for the incremental path: `pull_activities`
    # derives its resume point from MAX(start_date) in the table, which
    # self-heals if rows are deleted. The backfill walk keeps its own
    # cursor (`sync.BACKFILL_BEFORE_KEY`) because it moves in the opposite
    # direction and has nothing in the table to anchor to.
    cursor_key = None

    @property
    def name(self) -> str:
        return "strava"

    @property
    def display_name(self) -> str:
        return "Strava"

    def accounts(self, session: Session) -> list[OAuthToken]:
        """One account per connected Strava athlete.

        Rows needing re-auth are excluded rather than left to fail: a revoked
        grant would otherwise raise on every scheduled run, burying a real
        failure on the other athlete's row under a permanent one on this.
        `strava_status` and the dashboard still report the row as needing
        attention.
        """
        return (
            session.query(OAuthToken)
            .filter(
                OAuthToken.provider == PROVIDER,
                OAuthToken.needs_reauth_at.is_(None),
            )
            .all()
        )

    def account_user_id(self, account: OAuthToken) -> int | None:
        return account.user_id

    def account_label(self, account: OAuthToken) -> str:
        return f"athlete {account.account_email}"

    def pull(
        self, account: OAuthToken, session: Session, cursor: str | None
    ) -> PullResult:
        # No user_id stamping loop here (unlike the _template): the owning
        # user is already baked into every record by `parse_activity`, which
        # takes it as a keyword argument rather than inferring it later.
        return pull_activities(account, session, cursor)

    def store(self, session: Session, records: list[dict]) -> int:
        return store_activities(session, records)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Connection and coverage summary, per athlete, for the dashboard."""
        from sqlalchemy import func as sa_func

        from app.db import get_db
        from app.integrations.strava.models import StravaActivity

        db = get_db()
        with db.session() as session:
            tokens = session.query(OAuthToken).filter_by(provider=PROVIDER).all()
            athletes = []
            for token in tokens:
                count, last = (
                    session.query(
                        sa_func.count(StravaActivity.id),
                        sa_func.max(StravaActivity.start_date),
                    )
                    .filter(StravaActivity.user_id == token.user_id)
                    .one()
                )
                athletes.append({
                    "athlete_id": token.account_email,
                    "needs_reauth": token.needs_reauth_at is not None,
                    "activities": count,
                    "latest": last.strftime("%Y-%m-%d") if last else None,
                })
            total = session.query(sa_func.count(StravaActivity.id)).scalar() or 0

        return {"connected_athletes": len(tokens), "total_activities": total, "athletes": athletes}
