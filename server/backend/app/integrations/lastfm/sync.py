"""Sync logic: poll Last.fm API → upsert scrobbles into Postgres."""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

import httpx

from app.plugin.bases import PullResult
from app.plugin.config_store import plugin_config
from app.integrations.lastfm.client import (
    _classify_httpx_error,
    fetch_all_scrobbles,
    fetch_artist_tags,
    fetch_recent_tracks,
)
from app.integrations.lastfm.models import ArtistTag, Scrobble
from app.models.tokens import SyncState

logger = logging.getLogger(__name__)

# Commit every N scrobbles during backfill to keep memory bounded
COMMIT_BATCH = 500

# SyncState metadata keys for backfill cursor
_BACKFILL_PAGE_KEY = "lastfm_backfill_page"
_BACKFILL_STATUS_KEY = "lastfm_backfill_status"  # running / complete / interrupted


def pull_recent_scrobbles(
    session: Session, cursor: str | None, *, username: str, user_id: int
) -> PullResult:
    """Fetch tracks since this user's most recent scrobble. No DB writes — see
    `store_scrobbles`.

    For ongoing 15-minute sync. `cursor` is accepted for interface parity
    with `SourceIntegration.pull()` but unused — the "since" point is derived
    from the max `played_at` already in the DB, not a persisted cursor.

    That max is scoped to `user_id`. It has to be: a global MAX would take the
    most recently-active listener's timestamp and apply it to everyone, so a
    second user joining an established deployment would silently never
    backfill anything older than the first user's latest play.
    """
    # Find this user's most recent scrobble timestamp
    latest = (
        session.query(sa_func.max(Scrobble.played_at))
        .filter(Scrobble.user_id == user_id)
        .scalar()
    )

    from_ts = None
    if latest:
        # Add 1 second to avoid re-fetching the last scrobble
        from_ts = int(latest.timestamp()) + 1

    try:
        result = fetch_recent_tracks(
            api_key=plugin_config("lastfm").lastfm_api_key,
            username=username,
            from_timestamp=from_ts,
        )
    except httpx.HTTPError as exc:
        # Unlike backfill_scrobbles (which has its own retry+backoff via
        # _fetch_with_retry), the scheduled sync calls the API directly —
        # classify so the scheduler can tell "try again shortly" from "the
        # API key is bad, stop retrying".
        raise _classify_httpx_error(exc, "Last.fm sync_recent") from exc

    tracks = result["tracks"]
    if not tracks:
        logger.info("Last.fm sync: no new scrobbles for %s", username)
        return PullResult(records=[])

    # If there are more pages, paginate through all of them
    total_pages = result["pagination"]["total_pages"]
    if total_pages > 1:
        for page in range(2, total_pages + 1):
            try:
                page_result = fetch_recent_tracks(
                    api_key=plugin_config("lastfm").lastfm_api_key,
                    username=username,
                    from_timestamp=from_ts,
                    page=page,
                )
            except httpx.HTTPError as exc:
                raise _classify_httpx_error(exc, "Last.fm sync_recent") from exc
            tracks.extend(page_result["tracks"])

    # Stamp the owning user onto every record: `SourceIntegration.store()`
    # receives only `records`, not the account, so the owner has to travel
    # with the row (same pattern as google_mail's `pull_mail`).
    for track in tracks:
        track["user_id"] = user_id

    return PullResult(records=tracks)


def store_scrobbles(session: Session, records: list[dict]) -> int:
    """Persist `records` (raw Last.fm track dicts) — upsert only, no
    outbound I/O. A `records` of `[]` is a no-op.

    Each record carries its own `user_id` (stamped by `pull_recent_scrobbles`).
    Records are grouped by owner rather than assuming one user per call, so a
    caller that batches two accounts together still attributes correctly.
    """
    if not records:
        return 0

    by_user: dict[int, list[dict]] = {}
    for record in records:
        by_user.setdefault(record.get("user_id", 1), []).append(record)

    inserted = 0
    for user_id, tracks in by_user.items():
        inserted += _upsert_scrobbles(tracks, session, user_id=user_id)
    session.commit()
    logger.info(f"Last.fm sync: {inserted} new scrobbles")
    return inserted


def _backfill_key(user_id: int) -> str:
    """`sync_state.integration` key for one user's backfill cursor.

    Per-user, because the cursor is a resume page: a shared row would let one
    person's interrupted backfill resume the *other* person's walk from a page
    number that means nothing in their history.
    """
    return f"lastfm_backfill:{user_id}"


def _get_backfill_state(session: Session, user_id: int) -> SyncState:
    """Get or create this user's backfill cursor row in sync_state."""
    key = _backfill_key(user_id)
    state = session.query(SyncState).filter_by(integration=key).first()
    if not state:
        state = SyncState(integration=key, last_sync_status="never")
        session.add(state)
        session.flush()
    return state


def _save_backfill_progress(
    session: Session, user_id: int, page: int, total_pages: int, status: str
) -> None:
    """Persist backfill cursor so we can resume after failure."""
    state = _get_backfill_state(session, user_id)
    state.last_sync_status = status
    state.last_error = f"page={page}/{total_pages}"
    state.last_sync_at = datetime.now(timezone.utc)
    session.commit()


def backfill_scrobbles(
    session: Session, resume: bool = True, *, user_id: int | None = None
) -> int:
    """Fetch ALL scrobbles from the beginning of time, for one user.

    Paginates through everything, commits every COMMIT_BATCH.
    Saves progress after each batch so backfill can resume on failure.
    Skips duplicates via the unique constraint.

    Args:
        resume: If True (default), resume from the last saved page.
                If False, restart from page 1.
        user_id: Whose history to backfill. Defaults to the calling user, so
                 the MCP tool backfills the caller rather than whoever happens
                 to be configured first.

    Returns total number of new scrobbles inserted.
    """
    from app.auth.context import current_user_id
    from app.errors import PermanentError
    from app.integrations.lastfm import resolve_accounts

    if user_id is None:
        user_id = current_user_id()

    account = next(
        (a for a in resolve_accounts(session) if a.user_id == user_id), None
    )
    if account is None:
        raise PermanentError(
            f"No Last.fm username configured for user_id={user_id} — "
            "add them to the lastfm_usernames config map."
        )

    # Determine start page from saved cursor
    start_page = 1
    if resume:
        state = _get_backfill_state(session, user_id)
        if state.last_error and state.last_sync_status in ("running", "interrupted"):
            try:
                # Parse "page=250/500"
                page_str = state.last_error.split("=")[1].split("/")[0]
                start_page = int(page_str)
                logger.info(f"Resuming Last.fm backfill from page {start_page}")
            except (IndexError, ValueError):
                start_page = 1

    logger.info(
        f"Starting Last.fm backfill for {account.username} "
        f"(comar user {account.user_name}, from page {start_page})"
    )
    _save_backfill_progress(session, user_id, start_page, 0, "running")

    total_inserted = 0
    batch_buffer: list[dict] = []
    last_page = start_page

    def _count_for_user() -> int:
        return (
            session.query(sa_func.count(Scrobble.id))
            .filter(Scrobble.user_id == user_id)
            .scalar()
        )

    for page_tracks, current_page, total_pages in fetch_all_scrobbles(
        api_key=plugin_config("lastfm").lastfm_api_key,
        username=account.username,
        start_page=start_page,
    ):
        batch_buffer.extend(page_tracks)
        last_page = current_page

        # Commit in chunks and save progress
        while len(batch_buffer) >= COMMIT_BATCH:
            chunk = batch_buffer[:COMMIT_BATCH]
            batch_buffer = batch_buffer[COMMIT_BATCH:]
            inserted = _upsert_scrobbles(chunk, session, user_id=user_id)
            session.commit()
            total_inserted += inserted
            _save_backfill_progress(session, user_id, current_page, total_pages, "running")
            logger.info(
                f"Backfill progress: page {current_page}/{total_pages}, "
                f"{total_inserted} new, {_count_for_user()} total in DB"
            )

    # Flush remaining
    if batch_buffer:
        inserted = _upsert_scrobbles(batch_buffer, session, user_id=user_id)
        session.commit()
        total_inserted += inserted

    _save_backfill_progress(session, user_id, last_page, last_page, "complete")
    logger.info(
        f"Backfill complete: {total_inserted} new scrobbles, "
        f"{_count_for_user()} total in DB for {account.user_name}"
    )
    return total_inserted


def get_backfill_status(session: Session, *, user_id: int | None = None) -> dict:
    """Return the calling user's backfill state for the MCP status tool."""
    from app.auth.context import current_user_id

    if user_id is None:
        user_id = current_user_id()

    state = _get_backfill_state(session, user_id)
    result = {
        "status": state.last_sync_status,
        "last_updated": state.last_sync_at.isoformat() if state.last_sync_at else None,
    }
    if state.last_error and "page=" in (state.last_error or ""):
        try:
            parts = state.last_error.split("=")[1].split("/")
            result["current_page"] = int(parts[0])
            result["total_pages"] = int(parts[1]) if len(parts) > 1 else None
        except (IndexError, ValueError):
            pass
    result["total_scrobbles"] = (
        session.query(sa_func.count(Scrobble.id))
        .filter(Scrobble.user_id == user_id)
        .scalar()
    )
    return result


def enrich_artist_tags(session: Session, limit: int = 200) -> int:
    """Fetch genre tags for artists that don't have any yet.

    Calls Last.fm artist.getTopTags for each un-enriched artist.
    Rate-limited to ~5 req/sec to stay within Last.fm's limits.
    Returns count of artists enriched.
    """
    # Find artists in scrobbles that have no tags yet
    tagged_subq = (
        session.query(ArtistTag.artist_name_lower)
        .distinct()
        .subquery()
    )
    untagged = (
        session.query(Scrobble.artist_name)
        .distinct()
        .filter(
            sa_func.lower(Scrobble.artist_name).notin_(
                session.query(tagged_subq.c.artist_name_lower)
            )
        )
        .limit(limit)
        .all()
    )

    if not untagged:
        logger.info("Artist tag enrichment: all artists already tagged")
        return 0

    enriched = 0
    for (artist_name,) in untagged:
        tags = fetch_artist_tags(
            api_key=plugin_config("lastfm").lastfm_api_key,
            artist=artist_name,
        )

        artist_lower = artist_name.lower()

        # Check if this artist (by lowercase name) is already tagged —
        # covers case variants that map to the same canonical name
        already_tagged = (
            session.query(ArtistTag.id)
            .filter_by(artist_name_lower=artist_lower)
            .first()
        )
        if already_tagged:
            enriched += 1
            time.sleep(0.05)  # brief pause, skip API call
            continue

        if tags:
            for t in tags:
                session.add(ArtistTag(
                    artist_name=artist_name,
                    artist_name_lower=artist_lower,
                    tag=t["tag"],
                    weight=t["weight"],
                ))
        else:
            session.add(ArtistTag(
                artist_name=artist_name,
                artist_name_lower=artist_lower,
                tag="_no_tags",
                weight=0,
            ))

        # Flush immediately so subsequent checks see these rows
        session.flush()
        enriched += 1

        # Commit every 50 artists
        if enriched % 50 == 0:
            session.commit()
            logger.info(f"Artist tag enrichment: {enriched}/{len(untagged)} artists processed")

        # ~5 req/sec to stay within Last.fm rate limits
        time.sleep(0.2)

    session.commit()
    logger.info(f"Artist tag enrichment complete: {enriched} artists processed")
    return enriched


def _upsert_scrobbles(
    tracks: list[dict], session: Session, *, user_id: int
) -> int:
    """Insert scrobbles for one user, skipping duplicates. Returns new inserts.

    `user_id` is required rather than defaulting: a default silently attributes
    a second listener's history to the first user, and the duplicate check
    below is user-scoped, so a wrong id also defeats deduplication.
    """
    inserted = 0
    for t in tracks:
        played_at = datetime.fromtimestamp(t["played_at_uts"], tz=timezone.utc)

        # Composite unique on (user_id, track, artist, played_at) — same user
        # could in theory have two scrobbles at the same instant from
        # different devices; in practice Last.fm dedupes upstream.
        existing = (
            session.query(Scrobble.id)
            .filter_by(
                user_id=user_id,
                track_name=t["track_name"],
                artist_name=t["artist_name"],
                played_at=played_at,
            )
            .first()
        )
        if existing:
            continue

        session.add(
            Scrobble(
                user_id=user_id,
                track_name=t["track_name"],
                artist_name=t["artist_name"],
                album_name=t["album_name"],
                album_art_url=t["album_art_url"],
                played_at=played_at,
                mbid=t["mbid"],
                loved=t["loved"],
            )
        )
        inserted += 1

    return inserted
