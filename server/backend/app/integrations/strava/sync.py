"""Strava sync — token refresh, incremental pull, resumable historical backfill.

Split per the integration contract: `pull_*` does outbound I/O and no DB
writes; `store_*` does DB writes and no outbound I/O. `backfill_activities`
deliberately breaks that symmetry (it interleaves fetch and persist) because
a full history walk must checkpoint as it goes — see its docstring.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.auth.encryption import decrypt_token, encrypt_token
from app.errors import NeedsReauthError, PermanentError
from app.integrations.strava import client as strava_client
from app.integrations.strava.models import StravaActivity
from app.models.tokens import OAuthToken
from app.plugin.bases import PullResult
from app.plugin.config_store import plugin_config
from app.plugin.sync_runtime import SyncCursor

logger = logging.getLogger(__name__)

PROVIDER = "strava"

#: Cursor keys in `sync_cursors`, scoped per user by `SyncCursor`.
BACKFILL_BEFORE_KEY = "backfill_before"   # epoch seconds; walk backwards from here
BACKFILL_STATUS_KEY = "backfill_status"   # running / complete / interrupted

#: Refresh a little before the token actually dies. Strava issues six-hour
#: access tokens, so this is a rounding error in cost and removes the race
#: where a token valid at the check has expired by the time the request lands.
REFRESH_SKEW = timedelta(minutes=10)

#: Commit every N activities during backfill, so an interruption loses at
#: most one batch rather than the whole walk.
COMMIT_BATCH = 200


def _credentials() -> tuple[str, str]:
    """Return `(client_id, client_secret)`, or raise naming what's missing.

    Deliberately fails *here*, at the one call site that needs the values,
    rather than via `required=True` in the manifest's config schema.
    Marking them required would flip `is_configured()` to False and gate the
    entire integration off — including the read-only tools over activities
    already in Postgres, which need no credentials at all. That trade-off is
    written up in `server/CLAUDE.md` under "Platform vs personalisation".
    """
    config = plugin_config("strava")
    client_id = (config.strava_client_id or "").strip()
    client_secret = (config.strava_client_secret or "").strip()

    missing = [
        key
        for key, value in (
            ("strava_client_id", client_id),
            ("strava_client_secret", client_secret),
        )
        if not value
    ]
    if missing:
        raise PermanentError(
            "Strava is not configured: missing "
            + ", ".join(missing)
            + ". Register an API application at https://www.strava.com/settings/api "
            "and set these via PUT /api/integrations/strava/config."
        )
    return client_id, client_secret


def access_token_for(session: Session, token: OAuthToken) -> str:
    """Return a usable access token for `token`, refreshing if it is near expiry.

    ⚠️ Strava **rotates the refresh token** on every refresh, so the whole
    response is written back, not just `access_token`. Persisting only the
    access token produces a row that works for six hours and is then
    permanently dead — a failure that surfaces long after, and nowhere near,
    the code that caused it.

    The write is committed *before* the token is returned. If the caller then
    crashes mid-sync, the rotated refresh token is already saved; the reverse
    order would discard it and strand the grant.
    """
    if token.needs_reauth_at is not None:
        raise NeedsReauthError(
            token.account_email, token.needs_reauth_reason or "grant revoked"
        )

    now = datetime.now(timezone.utc)
    expires_at = token.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is not None and expires_at - REFRESH_SKEW > now:
        return decrypt_token(token.access_token)

    if not token.refresh_token:
        raise NeedsReauthError(token.account_email, "no refresh token stored")

    client_id, client_secret = _credentials()
    try:
        payload = strava_client.refresh_access_token(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=decrypt_token(token.refresh_token),
            account=token.account_email,
        )
    except NeedsReauthError as exc:
        # Stamp the row so the scheduler stops retrying a grant that will
        # never come back, and the dashboard says why.
        token.needs_reauth_at = datetime.now(timezone.utc)
        token.needs_reauth_reason = getattr(exc, "reason", str(exc))[:200]
        session.commit()
        raise

    token.access_token = encrypt_token(payload["access_token"])
    if payload.get("refresh_token"):
        token.refresh_token = encrypt_token(payload["refresh_token"])
    if payload.get("expires_at"):
        token.expires_at = datetime.fromtimestamp(payload["expires_at"], tz=timezone.utc)
    token.needs_reauth_at = None
    token.needs_reauth_reason = None
    session.commit()

    logger.info("Strava: refreshed access token for athlete %s", token.account_email)
    return payload["access_token"]


def _latlng(value: Any) -> tuple[float | None, float | None]:
    """Strava returns `[lat, lng]`, or `[]`/`None` for an activity with no GPS."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    return None, None


def _content_hash(raw: dict[str, Any]) -> str:
    """Stable hash over the fields that make an activity's *content*.

    Excludes the social counters (kudos, achievements) deliberately: those
    change whenever someone else clicks something, and including them would
    mark every old activity as "changed" on every sync.
    """
    significant = {
        key: raw.get(key)
        for key in (
            "name", "sport_type", "type", "start_date", "distance",
            "moving_time", "elapsed_time", "total_elevation_gain",
            "average_heartrate", "average_watts", "gear_id",
        )
    }
    blob = json.dumps(significant, sort_keys=True, default=str).encode()
    return hashlib.md5(blob).hexdigest()


def parse_activity(raw: dict[str, Any], *, user_id: int) -> dict[str, Any]:
    """Map one SummaryActivity onto `StravaActivity` column values.

    Units are carried across unchanged — metres, seconds, m/s. Conversion
    happens at the tool boundary (`tools.py`), never on ingest.
    """
    start_lat, start_lng = _latlng(raw.get("start_latlng"))
    end_lat, end_lng = _latlng(raw.get("end_latlng"))

    start_date = raw.get("start_date")
    parsed_start = (
        datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        if isinstance(start_date, str)
        else start_date
    )

    return {
        "user_id": user_id,
        "strava_id": int(raw["id"]),
        "name": (raw.get("name") or "")[:255],
        "sport_type": raw.get("sport_type"),
        "activity_type": raw.get("type"),
        "start_date": parsed_start,
        "timezone_name": (raw.get("timezone") or None),
        "utc_offset_seconds": (
            int(raw["utc_offset"]) if raw.get("utc_offset") is not None else None
        ),
        "distance_m": float(raw.get("distance") or 0.0),
        "moving_time_s": int(raw.get("moving_time") or 0),
        "elapsed_time_s": int(raw.get("elapsed_time") or 0),
        "total_elevation_gain_m": float(raw.get("total_elevation_gain") or 0.0),
        "average_speed_ms": raw.get("average_speed"),
        "max_speed_ms": raw.get("max_speed"),
        "average_cadence": raw.get("average_cadence"),
        "average_heartrate": raw.get("average_heartrate"),
        "max_heartrate": raw.get("max_heartrate"),
        "average_watts": raw.get("average_watts"),
        "weighted_average_watts": raw.get("weighted_average_watts"),
        "max_watts": raw.get("max_watts"),
        "device_watts": raw.get("device_watts"),
        "kilojoules": raw.get("kilojoules"),
        "suffer_score": raw.get("suffer_score"),
        "trainer": bool(raw.get("trainer")),
        "commute": bool(raw.get("commute")),
        "manual": bool(raw.get("manual")),
        "private": bool(raw.get("private")),
        "gear_id": raw.get("gear_id"),
        "device_name": (raw.get("device_name") or None),
        "kudos_count": int(raw.get("kudos_count") or 0),
        "achievement_count": int(raw.get("achievement_count") or 0),
        "pr_count": int(raw.get("pr_count") or 0),
        "start_lat": start_lat,
        "start_lng": start_lng,
        "end_lat": end_lat,
        "end_lng": end_lng,
        "map_polyline": (raw.get("map") or {}).get("summary_polyline"),
        "source_id": str(raw["id"]),
        "source_ts": parsed_start,
        "content_hash": _content_hash(raw),
    }


def store_activities(session: Session, records: list[dict]) -> int:
    """Upsert parsed activity dicts. No outbound I/O.

    Returns the number of rows written (inserted + genuinely updated).
    Unchanged rows are skipped via `content_hash`, so a re-run over the same
    window reports 0 rather than restating the whole history as "synced" —
    the count means something.

    Records are grouped by `user_id` rather than assuming one owner per call,
    matching `lastfm.store_scrobbles`: a caller that batches two athletes
    together still attributes each row correctly.
    """
    if not records:
        return 0

    by_user: dict[int, list[dict]] = {}
    for record in records:
        by_user.setdefault(record["user_id"], []).append(record)

    written = 0
    for user_id, rows in by_user.items():
        strava_ids = [row["strava_id"] for row in rows]
        existing = {
            activity.strava_id: activity
            for activity in session.query(StravaActivity)
            .filter(
                StravaActivity.user_id == user_id,
                StravaActivity.strava_id.in_(strava_ids),
            )
            .all()
        }

        for row in rows:
            current = existing.get(row["strava_id"])
            if current is None:
                session.add(StravaActivity(**row))
                written += 1
                continue
            if current.content_hash == row["content_hash"]:
                # Social counters may still have moved; keep them current
                # without counting the row as changed.
                current.kudos_count = row["kudos_count"]
                current.achievement_count = row["achievement_count"]
                current.pr_count = row["pr_count"]
                current.synced_at = datetime.now(timezone.utc)
                continue
            for key, value in row.items():
                setattr(current, key, value)
            current.synced_at = datetime.now(timezone.utc)
            written += 1

    session.commit()
    return written


def pull_activities(
    account: OAuthToken, session: Session, cursor: str | None = None
) -> PullResult:
    """Fetch activities newer than this athlete's most recent stored one.

    `cursor` is accepted for `SourceIntegration.pull()` interface parity but
    unused: the resume point is derived from `MAX(start_date)` for this user,
    which is self-healing. A persisted cursor would keep pointing past a row
    that was later deleted.

    That MAX is scoped to `user_id` — a global MAX would take the more active
    athlete's latest ride and apply it to everyone, so a second athlete
    joining an established deployment would silently never pull anything
    older than the first athlete's last activity. (Exactly the bug
    `lastfm.pull_recent_scrobbles` documents having had.)
    """
    access_token = access_token_for(session, account)

    latest = (
        session.query(sa_func.max(StravaActivity.start_date))
        .filter(StravaActivity.user_id == account.user_id)
        .scalar()
    )

    after = None
    if latest is not None:
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        # Strava's `after` is exclusive-ish and second-resolution; stepping
        # back a second re-fetches the boundary activity, which the
        # content-hash upsert makes free, rather than risking skipping it.
        after = int(latest.timestamp()) - 1

    records: list[dict] = []
    page = 1
    while True:
        batch = strava_client.fetch_activities(
            access_token=access_token,
            after=after,
            page=page,
            account=account.account_email,
        )
        if not batch:
            break
        records.extend(parse_activity(raw, user_id=account.user_id) for raw in batch)
        if len(batch) < strava_client.MAX_PER_PAGE:
            break
        page += 1

    logger.info(
        "Strava: pulled %d activities for athlete %s", len(records), account.account_email
    )
    return PullResult(records=records)


def backfill_activities(
    session: Session, *, user_id: int, resume: bool = True, max_pages: int = 100
) -> dict[str, Any]:
    """Walk an athlete's entire history, oldest-ward, checkpointing as it goes.

    Interleaves fetch and store rather than obeying the usual pull/store
    split. That is the point: a full history walk can be interrupted by a
    rate limit or a restart, and a design that only persisted at the end
    would throw away every request it had already spent.

    **Walks backwards using `before`, not forwards using `page`.** Page
    numbers are computed against a list that changes whenever a new activity
    is uploaded, so a paginated walk silently *skips* activities when the
    list shifts under it. Anchoring each request to `before=<oldest start
    seen so far>` makes the walk stable against concurrent uploads: the
    window only ever moves further into the settled past.

    Terminates on an empty page, or on a page that yields no id not already
    seen in this run — the latter guards the pathological case of several
    activities sharing an identical start timestamp, which would otherwise
    have the `before` cursor stop advancing and loop forever.

    Returns a status dict; `status` is `complete` only when Strava actually
    returned an empty page, never merely because the loop ended.
    """
    _credentials()  # fail fast and loudly if the app isn't registered yet

    token = (
        session.query(OAuthToken)
        .filter_by(provider=PROVIDER, user_id=user_id)
        .first()
    )
    if token is None:
        raise PermanentError(
            f"No Strava account connected for user {user_id}. "
            "Visit /api/strava/connect to authorise."
        )

    before: int | None = None
    if resume:
        saved = SyncCursor.get(session, "strava", BACKFILL_BEFORE_KEY, user_id=user_id)
        if saved:
            before = int(saved)
    if before is None:
        before = int(datetime.now(timezone.utc).timestamp())

    SyncCursor.set(session, "strava", BACKFILL_STATUS_KEY, "running", user_id=user_id)

    seen: set[int] = set()
    total_written = 0
    total_fetched = 0
    status = "interrupted"
    pages = 0

    try:
        while pages < max_pages:
            access_token = access_token_for(session, token)
            batch = strava_client.fetch_activities(
                access_token=access_token, before=before, account=token.account_email
            )
            pages += 1

            if not batch:
                status = "complete"
                break

            new_raw = [raw for raw in batch if int(raw["id"]) not in seen]
            if not new_raw:
                logger.warning(
                    "Strava backfill: page yielded no new ids at before=%s — "
                    "stopping to avoid a loop on identical start timestamps",
                    before,
                )
                status = "stalled"
                break

            seen.update(int(raw["id"]) for raw in new_raw)
            total_fetched += len(new_raw)

            parsed = [parse_activity(raw, user_id=user_id) for raw in new_raw]
            total_written += store_activities(session, parsed)

            oldest = min(record["start_date"] for record in parsed)
            before = int(oldest.timestamp())
            SyncCursor.set(
                session, "strava", BACKFILL_BEFORE_KEY, str(before), user_id=user_id
            )
        else:
            # Loop exhausted `max_pages` without an empty page — more history
            # remains. Reported honestly rather than as completion.
            status = "interrupted"
    except strava_client.RateLimited as exc:
        logger.warning(
            "Strava backfill paused on rate limit (usage=%s of %s) — "
            "cursor saved at before=%s, re-run to resume",
            exc.usage, exc.limit, before,
        )
        status = "rate_limited"

    SyncCursor.set(session, "strava", BACKFILL_STATUS_KEY, status, user_id=user_id)

    logger.info(
        "Strava backfill for user %s: %s — %d fetched, %d written across %d pages",
        user_id, status, total_fetched, total_written, pages,
    )
    return {
        "status": status,
        "pages": pages,
        "fetched": total_fetched,
        "written": total_written,
        "resume_before": before,
        "athlete": token.account_email,
    }
