"""Last.fm integration — scrobble history sync and listening stats."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as sa_func

from app.config import settings
from app.db import get_db
from app.integrations.base import BaseIntegration
from app.integrations.lastfm.models import Scrobble
from app.integrations.lastfm.sync import sync_recent
from app.integrations.lastfm.tools import get_mcp_tools

logger = logging.getLogger(__name__)


class LastfmIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "lastfm"

    @property
    def display_name(self) -> str:
        return "Last.fm"

    def sync(self) -> None:
        """Sync recent scrobbles from Last.fm."""
        db = get_db()
        with db.session() as session:
            count = sync_recent(session)
            logger.info(f"Last.fm sync complete: {count} new scrobbles")

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return scrobble summary for the dashboard."""
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

    def sync_schedule(self) -> str | None:
        return "*/15 * * * *"  # Every 15 minutes

    def is_configured(self) -> bool:
        return bool(settings.lastfm_api_key and settings.lastfm_username)
