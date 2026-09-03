"""Last.fm integration — scrobble history sync and listening stats.

`SourceIntegration` conversion (V4 chunk 4.3, batch A). **Multi-user**: one
account per entry in the `lastfm_usernames` config map, fanned out by the
inherited `sync()`. Each account carries its own `user_id`, so two people on
one deployment keep separate `scrobbles` rows (the table is `UserOwnedMixin`)
and each other's listening never shows up in the other's tools.

`sync()` itself is entirely inherited — this class supplies only
`accounts()`/`pull()`/`store()`, tool wiring, and dashboard data.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.integrations.lastfm.models import Scrobble
from app.integrations.lastfm.sync import pull_recent_scrobbles, store_scrobbles
from app.integrations.lastfm.tools import get_mcp_tools
from app.plugin.bases import PullResult, SourceIntegration
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LastfmAccount:
    """One (comar user, Last.fm username) pairing to sync."""

    user_id: int
    user_name: str
    username: str


def resolve_accounts(session: Session) -> list[LastfmAccount]:
    """Build the account list from config, resolving comar user names to ids.

    Prefers the `lastfm_usernames` map. Falls back to the deprecated single
    `lastfm_username`, attributed to the lowest-id active user — that keeps a
    pre-upgrade deployment syncing without a config edit, and the id ordering
    makes the attribution deterministic rather than "whichever row came back
    first". A name in the map that doesn't match a `users` row is skipped with
    a warning rather than failing the whole sync: one typo shouldn't stop the
    other person's scrobbles from syncing.
    """
    from app.models.users import User

    cfg = plugin_config("lastfm")
    mapping = dict(cfg.lastfm_usernames or {})

    if not mapping:
        legacy = (cfg.lastfm_username or "").strip()
        if not legacy:
            return []
        fallback = (
            session.query(User)
            .filter(User.is_active.is_(True))
            .order_by(User.id)
            .first()
        )
        if fallback is None:
            logger.warning("Last.fm: no active users — nothing to sync")
            return []
        logger.info(
            "Last.fm: using deprecated lastfm_username, attributed to user %r. "
            "Set lastfm_usernames to sync more than one person.",
            fallback.name,
        )
        return [LastfmAccount(user_id=fallback.id, user_name=fallback.name, username=legacy)]

    accounts: list[LastfmAccount] = []
    for user_name, username in mapping.items():
        username = (username or "").strip()
        if not username:
            continue
        row = session.query(User).filter_by(name=user_name).first()
        if row is None:
            logger.warning(
                "Last.fm: config names unknown comar user %r — skipping", user_name
            )
            continue
        if not row.is_active:
            logger.info("Last.fm: user %r is inactive — skipping", user_name)
            continue
        accounts.append(
            LastfmAccount(user_id=row.id, user_name=row.name, username=username)
        )
    return accounts


class LastfmIntegration(SourceIntegration):
    @property
    def name(self) -> str:
        return "lastfm"

    @property
    def display_name(self) -> str:
        return "Last.fm"

    def accounts(self, session: Session) -> list[LastfmAccount]:
        accounts = resolve_accounts(session)
        if not accounts:
            logger.info("No Last.fm usernames configured — skipping scrobble sync")
        return accounts

    def account_user_id(self, account: LastfmAccount) -> int | None:
        return account.user_id

    def account_label(self, account: LastfmAccount) -> str:
        return f"{account.user_name} ({account.username})"

    def pull(self, account: LastfmAccount, session: Session, cursor: str | None) -> PullResult:
        return pull_recent_scrobbles(
            session, cursor, username=account.username, user_id=account.user_id
        )

    def store(self, session: Session, records: list[dict]) -> int:
        return store_scrobbles(session, records)

    def is_configured(self) -> bool:
        """API key plus at least one username, in either config form.

        Overridden because the default implementation only checks `required`
        keys, and neither username key can be `required`: marking the map
        required would break a deployment still on the single-value form, and
        marking the single value required would break one that has migrated.
        """
        cfg = plugin_config("lastfm")
        if not cfg.lastfm_api_key:
            return False
        has_map = any((v or "").strip() for v in (cfg.lastfm_usernames or {}).values())
        return bool(has_map or (cfg.lastfm_username or "").strip())

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return scrobble summary for the dashboard."""
        from app.db import get_db

        db = get_db()
        with db.session() as session:
            total = session.query(sa_func.count(Scrobble.id)).scalar() or 0

            # Recent tracks (last 10)
            recent = (
                session.query(Scrobble)
                .order_by(Scrobble.played_at.desc())
                .limit(10)
                .all()
            )

            # Top artists this week
            now = datetime.now(timezone.utc)
            week_start = now - timedelta(days=now.weekday())
            week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

            top_artists_week = (
                session.query(
                    Scrobble.artist_name,
                    sa_func.count(Scrobble.id).label("play_count"),
                )
                .filter(Scrobble.played_at >= week_start)
                .group_by(Scrobble.artist_name)
                .order_by(sa_func.count(Scrobble.id).desc())
                .limit(10)
                .all()
            )

            return {
                "total_scrobbles": total,
                "recent_tracks": [
                    {
                        "track": s.track_name,
                        "artist": s.artist_name,
                        "album": s.album_name,
                        "album_art_url": s.album_art_url,
                        "played_at": s.played_at.isoformat() if s.played_at else None,
                        "loved": s.loved,
                    }
                    for s in recent
                ],
                "top_artists_this_week": [
                    {"artist": a, "play_count": c}
                    for a, c in top_artists_week
                ],
            }

    # is_configured() is overridden above — the default (all `required` keys
    # set) can't express "api key AND at least one of two username forms".
