"""Last.fm API client — fetches scrobble history via the public API."""

import logging
import time
from collections.abc import Generator
from typing import Any

import httpx

from app.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)

API_BASE = "http://ws.audioscrobbler.com/2.0/"
DEFAULT_LIMIT = 200
REQUEST_TIMEOUT = 30  # seconds per API request
MAX_RETRIES = 3
RETRY_BACKOFF = [2, 5, 15]  # seconds between retries


def _classify_httpx_error(exc: Exception, context: str) -> Exception:
    """Map an httpx failure to TransientError or PermanentError.

    Returns the exception to raise (chained `from exc` by the caller).
    Last.fm has no OAuth — a 401/403 here means a bad/missing API key, a
    config problem, not something a re-auth flow fixes, but still not worth
    retrying with the same (broken) key.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return PermanentError(f"{context}: HTTP {status} (check HOME_LASTFM_API_KEY)")
        if status == 429 or status >= 500:
            return TransientError(f"{context}: HTTP {status}")
        return exc
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError)):
        return TransientError(f"{context}: {exc}")
    return exc


def fetch_recent_tracks(
    api_key: str,
    username: str,
    from_timestamp: int | None = None,
    to_timestamp: int | None = None,
    page: int = 1,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Fetch a single page of recent tracks from Last.fm.

    Returns dict with 'tracks' (list of parsed track dicts) and
    'pagination' (total_pages, total, page).
    """
    params: dict[str, Any] = {
        "method": "user.getrecenttracks",
        "user": username,
        "api_key": api_key,
        "format": "json",
        "limit": limit,
        "page": page,
    }
    if from_timestamp is not None:
        params["from"] = from_timestamp
    if to_timestamp is not None:
        params["to"] = to_timestamp

    resp = httpx.get(API_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    recent = data.get("recenttracks", {})
    attr = recent.get("@attr", {})
    raw_tracks = recent.get("track", [])

    # API returns a single dict (not list) when there's exactly one track
    if isinstance(raw_tracks, dict):
        raw_tracks = [raw_tracks]

    tracks = []
    for t in raw_tracks:
        parsed = _parse_track(t)
        if parsed is not None:
            tracks.append(parsed)

    return {
        "tracks": tracks,
        "pagination": {
            "page": int(attr.get("page", 1)),
            "total_pages": int(attr.get("totalPages", 1)),
            "total": int(attr.get("total", 0)),
        },
    }


def _fetch_with_retry(
    api_key: str,
    username: str,
    from_timestamp: int | None = None,
    page: int = 1,
) -> dict[str, Any] | None:
    """Fetch a page with retry + exponential backoff. Returns None on exhausted retries."""
    for attempt in range(MAX_RETRIES):
        try:
            return fetch_recent_tracks(
                api_key, username, from_timestamp=from_timestamp, page=page
            )
        except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
            backoff = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            logger.warning(
                f"Last.fm page {page} attempt {attempt + 1}/{MAX_RETRIES} failed: {e} "
                f"— retrying in {backoff}s"
            )
            time.sleep(backoff)
    logger.error(f"Last.fm page {page}: all {MAX_RETRIES} retries exhausted")
    return None


def fetch_all_scrobbles(
    api_key: str,
    username: str,
    from_timestamp: int | None = None,
    start_page: int = 1,
) -> Generator[tuple[list[dict[str, Any]], int, int], None, None]:
    """Generator that paginates through ALL scrobbles, yielding batches.

    Each yielded item is (tracks, current_page, total_pages) — the caller
    can save current_page as a resume cursor.

    Args:
        start_page: Resume from this page (1-indexed). Default 1 = start from beginning.
    """
    page = start_page
    total_pages = None  # discovered on first request

    while total_pages is None or page <= total_pages:
        result = _fetch_with_retry(api_key, username, from_timestamp=from_timestamp, page=page)
        if result is None:
            # All retries exhausted — stop and let caller save progress
            break

        total_pages = result["pagination"]["total_pages"]
        tracks = result["tracks"]

        if tracks:
            yield tracks, page, total_pages

        logger.debug(f"Page {page}/{total_pages} — {len(tracks)} tracks")
        page += 1


def fetch_artist_tags(
    api_key: str,
    artist: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fetch top tags for an artist from Last.fm.

    Returns list of dicts with 'tag' (str) and 'weight' (int).
    Returns empty list on error (artist not found, API issues).
    """
    params = {
        "method": "artist.getTopTags",
        "artist": artist,
        "api_key": api_key,
        "format": "json",
    }
    try:
        resp = httpx.get(API_BASE, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.debug(f"Failed to fetch tags for '{artist}': {e}")
        return []

    # Last.fm returns {"error": ...} for unknown artists
    if "error" in data:
        return []

    raw_tags = data.get("toptags", {}).get("tag", [])
    if isinstance(raw_tags, dict):
        raw_tags = [raw_tags]

    # Deduplicate by tag name, keeping the highest weight
    seen: dict[str, int] = {}
    for t in raw_tags:
        name = t.get("name", "").strip().lower()
        count = int(t.get("count", 0))
        if name and count > 0 and len(name) <= 190:
            if name not in seen or count > seen[name]:
                seen[name] = count

    # Return top N by weight
    tags = sorted(seen.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [{"tag": name, "weight": weight} for name, weight in tags]


def _parse_track(track: dict) -> dict[str, Any] | None:
    """Parse a raw Last.fm track dict into a clean dict.

    Returns None for 'now playing' tracks (no date).
    """
    # Now-playing tracks have @attr.nowplaying = "true" and no date
    attr = track.get("@attr", {})
    if attr.get("nowplaying") == "true":
        return None

    date_info = track.get("date")
    if not date_info:
        return None

    # Get the largest image (last in the list)
    images = track.get("image", [])
    album_art = None
    if images:
        largest = images[-1].get("#text", "")
        if largest:
            album_art = largest

    return {
        "track_name": track.get("name", ""),
        "artist_name": track.get("artist", {}).get("#text", ""),
        "album_name": track.get("album", {}).get("#text", "") or None,
        "album_art_url": album_art,
        "played_at_uts": int(date_info.get("uts", 0)),
        "mbid": track.get("mbid") or None,
        "loved": track.get("loved", "0") == "1",
    }
