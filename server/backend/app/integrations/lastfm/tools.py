"""MCP tool definitions for Last.fm — migrated to declarative DSL."""

import json
import logging
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.integrations.lastfm.models import ArtistTag, Scrobble
from app.tools import CustomTool, ListTool, SearchTool, StatsTool
from app.tools.helpers import iso_or_none, period_since, scoped_query, serialize, top_n_group_by

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared formatter
# ---------------------------------------------------------------------------


def _scrobble_to_dict(s: Scrobble) -> dict:
    return serialize(
        s,
        ["track_name", "artist_name", "album_name", "album_art_url", "played_at", "mbid", "loved"],
        renames={"track_name": "track", "artist_name": "artist", "album_name": "album"},
        transforms={"played_at": iso_or_none},
    )


# ---------------------------------------------------------------------------
# Stats compute callback (complex domain logic stays explicit)
# ---------------------------------------------------------------------------

def _compute_stats(session: Session, arguments: dict[str, Any]) -> dict:
    period = arguments.get("period", "all_time")
    top_limit = min(int(arguments.get("limit", 10)), 50)

    since = period_since(period)

    base_query = scoped_query(session, Scrobble)
    if since:
        base_query = base_query.filter(Scrobble.played_at >= since)

    total_scrobbles = base_query.count()
    unique_artists = base_query.with_entities(
        sa_func.count(sa_func.distinct(Scrobble.artist_name))
    ).scalar()
    unique_tracks = base_query.with_entities(
        sa_func.count(sa_func.distinct(
            sa_func.concat(Scrobble.artist_name, " - ", Scrobble.track_name)
        ))
    ).scalar()

    top_artists_q = top_n_group_by(base_query, Scrobble.artist_name, top_limit, count_col=Scrobble.id, label="play_count")

    top_tracks_q = (
        base_query.with_entities(
            Scrobble.track_name,
            Scrobble.artist_name,
            sa_func.count(Scrobble.id).label("play_count"),
        )
        .group_by(Scrobble.track_name, Scrobble.artist_name)
        .order_by(sa_func.count(Scrobble.id).desc())
        .limit(top_limit)
        .all()
    )

    top_genres_q = (
        session.query(
            ArtistTag.tag,
            sa_func.count(Scrobble.id).label("play_count"),
        )
        .join(Scrobble, sa_func.lower(Scrobble.artist_name) == ArtistTag.artist_name_lower)
        .filter(ArtistTag.tag != "_no_tags", ArtistTag.weight >= 10)
    )
    if since:
        top_genres_q = top_genres_q.filter(Scrobble.played_at >= since)
    top_genres = (
        top_genres_q
        .group_by(ArtistTag.tag)
        .order_by(sa_func.count(Scrobble.id).desc())
        .limit(top_limit)
        .all()
    )

    # Artist tags lookup
    top_artist_names = [a.lower() for a, _ in top_artists_q]
    artist_tags_rows = (
        session.query(ArtistTag.artist_name_lower, ArtistTag.tag, ArtistTag.weight)
        .filter(
            ArtistTag.artist_name_lower.in_(top_artist_names),
            ArtistTag.tag != "_no_tags",
            ArtistTag.weight >= 10,
        )
        .order_by(ArtistTag.weight.desc())
        .all()
    )
    tags_by_artist: dict[str, list[str]] = {}
    for artist_lower, tag, _weight in artist_tags_rows:
        tags_by_artist.setdefault(artist_lower, [])
        if len(tags_by_artist[artist_lower]) < 5:
            tags_by_artist[artist_lower].append(tag)

    return {
        "period": period,
        "total_scrobbles": total_scrobbles,
        "unique_artists": unique_artists,
        "unique_tracks": unique_tracks,
        "top_artists": [
            {"artist": a, "play_count": c, "tags": tags_by_artist.get(a.lower(), [])}
            for a, c in top_artists_q
        ],
        "top_tracks": [
            {"track": t, "artist": a, "play_count": c}
            for t, a, c in top_tracks_q
        ],
        "top_genres": [
            {"genre": tag, "play_count": count}
            for tag, count in top_genres
        ],
    }


# ---------------------------------------------------------------------------
# Custom handlers (admin tools — stay hand-written)
# ---------------------------------------------------------------------------

def handle_backfill(session: Session, arguments: dict[str, Any]) -> str:
    from app.integrations.lastfm.sync import backfill_scrobbles
    try:
        count = backfill_scrobbles(session)
        total = session.query(sa_func.count(Scrobble.id)).scalar()
        return json.dumps({"status": "ok", "new_scrobbles": count, "total_cached": total})
    except Exception as e:
        logger.exception("Last.fm backfill failed")
        return json.dumps({"error": str(e)})


def handle_enrich(session: Session, arguments: dict[str, Any]) -> str:
    from app.integrations.lastfm.sync import enrich_artist_tags
    limit = int(arguments.get("limit", 200))
    try:
        count = enrich_artist_tags(session, limit=limit)
        total_tagged = (
            session.query(sa_func.count(sa_func.distinct(ArtistTag.artist_name_lower)))
            .filter(ArtistTag.tag != "_no_tags").scalar()
        ) or 0
        total_artists = (
            session.query(sa_func.count(sa_func.distinct(Scrobble.artist_name))).scalar()
        ) or 0
        return json.dumps({
            "status": "ok",
            "artists_enriched": count,
            "total_tagged": total_tagged,
            "total_artists": total_artists,
            "coverage": f"{total_tagged / total_artists * 100:.1f}%" if total_artists else "0%",
        })
    except Exception as e:
        logger.exception("Artist tag enrichment failed")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool definitions — declarative DSL
# ---------------------------------------------------------------------------

def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        # ── Declarative tools ──────────────────────────────────
        ListTool(
            name="lastfm_recent",
            description=(
                "Recent Last.fm scrobbles — what's been playing. Returns track name, "
                "artist, album, album art URL, and when it was played. "
                "Filter by date range for a specific period."
            ),
            model=Scrobble,
            timestamp_col="played_at",
            to_dict=_scrobble_to_dict,
            date_params=("from_date", "to_date"),
            category="music",
            examples=[
                "What have I been listening to?",
                "What was playing last night?",
            ],
        ).build(),

        SearchTool(
            name="lastfm_search",
            description=(
                "Search listening history by track or artist name. "
                "Case-insensitive partial matching across all cached scrobbles."
            ),
            model=Scrobble,
            search_columns=["track_name", "artist_name"],
            timestamp_col="played_at",
            to_dict=_scrobble_to_dict,
            date_params=("from_date", "to_date"),
            category="music",
            examples=[
                "Have I listened to Radiohead lately?",
                "Find plays of Bohemian Rhapsody",
            ],
        ).build(),

        StatsTool(
            name="lastfm_stats",
            description=(
                "Listening statistics: total scrobbles, unique artists/tracks, top artists "
                "with genre tags, top tracks, and top genres by play count. "
                "Supports calendar periods (this_week/this_month/this_year/all_time) "
                "and Last.fm-native rolling windows (7day/1month/3month/6month/12month/overall)."
            ),
            model=Scrobble,
            compute=_compute_stats,
            input_schema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Time period. Calendar: this_week, this_month, this_year, all_time. Rolling: 7day, 1month, 3month, 6month, 12month, overall.",
                        "enum": [
                            "this_week", "this_month", "this_year", "all_time",
                            "7day", "1month", "3month", "6month", "12month", "overall",
                        ],
                        "default": "all_time",
                    },
                    "limit": {
                        "type": ["integer", "string"],
                        "description": "Number of top artists/tracks to return (default 10, max 50).",
                        "default": 10,
                    },
                },
            },
            category="music",
            examples=[
                "What are my top artists this week?",
                "Show my listening stats",
                "What genres do I listen to most?",
            ],
        ).build(),

        # ── Custom tools (admin, stay hand-written) ────────────
        CustomTool(
            name="lastfm_backfill",
            description=(
                "Trigger a full Last.fm history backfill. Fetches ALL scrobbles from the "
                "beginning and caches them. Admin tool — may take a while for large libraries. "
                "Has resume support — safe to re-run if interrupted."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_backfill,
            category="music",
        ).build(),

        CustomTool(
            name="lastfm_enrich",
            description=(
                "Enrich artists with genre/style tags from Last.fm API. Fetches top tags "
                "for artists that don't have tags yet. Admin tool — rate-limited at ~0.2s "
                "per artist. Run after backfill to populate genre data for stats."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max artists to enrich in this batch (default 200). Each takes ~0.2s.",
                        "default": 200,
                    },
                },
            },
            handler=handle_enrich,
            category="music",
        ).build(),
    ]
