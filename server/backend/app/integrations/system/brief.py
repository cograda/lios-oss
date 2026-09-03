"""The daily brief — every read-only source the morning note needs, in one call.

## Why this exists

`/daily-note` step 4 used to enumerate 24 separate MCP tool calls. That cost
~12s, and almost none of it was query time: each call is its own HTTPS
round-trip (a WireGuard hop, since `.mcp.json` points at the tailnet URL),
re-validating a bearer, opening a session, going through `app.plugin.dispatch`
and writing a `tool_calls` audit row. Fanning in collapses 23 round-trips into
one without making any single query faster.

23, not 24: `snag_capture` **writes** (it registers snags and re-renders the
vault note and the Sheets mirror). It stays a separate call, and this tool
keeps `readOnlyHint: true` honestly.

## Three independent wins, in order of size

1. **Fan-in** — one round-trip instead of 23.
2. **Parallel fan-out** — sources run concurrently, so the brief costs about
   its slowest source rather than the sum. `handle_morning_briefing`'s
   sequential try/except chain is what this replaces.
3. **Pre-warm** — a cron builds the cacheable half before the user is awake,
   so the first `/daily-note` of the day reads a warm entry.

## What must not be cached

Caching is *selective*, and the split is not cosmetic. Rail departures and
Home Assistant's home status are real-time; serving those from a 15-minute
cache would show trains that have left, which is precisely the section that
has to be right. Anything marked `volatile=True` bypasses the cache in both
directions — never read from it, never written to it, and never pre-warmed.

There is a second, subtler category: **push-fed sources race their producer**.
Health data arrives whenever the phone's Health Auto Export decides to push —
the server cannot know whether a snapshot predates the night's data. A
pre-warm that runs minutes before the push caches `0h / no sessions`, the
first /daily-note of the day serves it as fresh, and the note reports a
sleepless night that never happened (2026-08-11: three consecutive notes
escalated a fictional "Watch not recording" alarm this way — the failure is
nasty precisely because it fails to a *plausible zero*, not to an error, so
the error-payload cache guard never fires). Health sources are therefore
volatile too. The cost is nil: every health read is a cheap local Postgres
query, so the cache was never buying anything there.

## The concurrency hazard

`current_user_id()` is a ContextVar, and `ThreadPoolExecutor` does **not**
propagate context to its workers. A worker that didn't re-pin would either
raise (the sentinel-0 guard in `app.auth.context`) or, far worse, inherit a
stale binding. Every worker therefore re-pins `use_user(user_id)` explicitly
and opens **its own** session — SQLAlchemy sessions are not thread-safe, and
sharing one across the fan-out is a data-corruption bug waiting to happen.
`tests/test_daily_brief.py` asserts both properties directly.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.auth.context import use_user
from app.plugin.capabilities import get_capability
from app.services import preferences as prefs_service

logger = logging.getLogger(__name__)

# How long a cached (non-volatile) source stays usable. Fifteen minutes is
# chosen against the pre-warm cadence, not plucked: the cron runs every 15
# minutes through the morning, so a warm entry is nearly always available and
# nothing served is older than one cron interval.
DEFAULT_MAX_AGE_SECONDS = 900

# Cap on concurrent workers. Above ~8 the bottleneck stops being latency and
# starts being the Postgres connection pool, which every worker draws from.
MAX_WORKERS = 8


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ctx:
    """Everything the argument builders need to shape their calls."""

    user_id: int
    prefs: dict[str, Any]
    since: str | None          # ISO timestamp of the previous note, if any
    today: date

    @property
    def is_weekend(self) -> bool:
        return self.today.weekday() >= 5

    @property
    def lookback_start(self) -> date:
        """How far back comms and previous notes should reach.

        Monday reaches back to Friday so the weekend isn't lost, and the
        weekend itself reaches back to Friday too. Tue–Fri use the user's
        `daily_note.lookback_days`.
        """
        weekday = self.today.weekday()
        if weekday == 0:                       # Monday
            return self.today - timedelta(days=3)
        if weekday == 5:                       # Saturday
            return self.today - timedelta(days=1)
        if weekday == 6:                       # Sunday
            return self.today - timedelta(days=2)
        days = max(1, int(self.prefs.get("daily_note.lookback_days", 2)))
        return self.today - timedelta(days=days)


@dataclass(frozen=True)
class Source:
    """One composed source in the brief."""

    key: str                   # where it lands in the payload
    capability: str            # resolved via get_capability()
    method: str                # facade method name
    args: dict[str, Any] = field(default_factory=dict)
    # Section this source feeds. None means "always fetch" (system alerts).
    # Used both for `daily_note.sections` and for /refresh-style filtering.
    section: str | None = None
    # Never cached, never pre-warmed — see the module docstring.
    volatile: bool = False
    # Capability whose facade exposes has_data(); when set and the user has
    # no data, the source is skipped entirely rather than fetched and thrown
    # away. This is what makes a section auto-omit for a user who doesn't
    # use that integration, with no configuration required.
    gated_on: str | None = None


def build_sources(ctx: Ctx) -> list[Source]:
    """The source list for this user, on this day, with their preferences."""
    p = ctx.prefs
    # Fallbacks mirror `preferences.PREFERENCES`; the registry is the source.
    mail_limit = max(1, int(p.get("comms.mail_limit", 25)))
    wa_limit = max(1, int(p.get("comms.whatsapp_limit", 30)))

    sources: list[Source] = [
        # Always fetched: if an integration is degraded, the caller needs to
        # know the rest of this payload may be stale before trusting it.
        Source("alerts", "system.alerts", "alerts", volatile=True),

        Source("calendar", "calendar.query", "today", section="today", volatile=True),

        Source("weather_current", "weather.query", "current"),
        Source("weather_forecast", "weather.query", "forecast", {"days": 2}),

        Source(
            "reminders", "reminders.query", "sync",
            {"since": ctx.since} if ctx.since else {},
            section="tasks", volatile=True,
        ),

        Source(
            "home", "homeassistant.entities", "home_status",
            section="house", volatile=True,
        ),

        Source(
            "mail_unread", "mail.query", "unread", {"limit": 10},
            section="email", gated_on="mail.query",
        ),
        Source(
            "mail_recent", "mail.query", "recent", {"limit": mail_limit},
            section="email", gated_on="mail.query",
        ),
        Source(
            "whatsapp", "whatsapp.query", "recent", {"limit": wa_limit},
            section="whatsapp", gated_on="whatsapp.query",
        ),
        Source(
            "attachments", "attachments.query", "pending",
            {"since_days": 7, "limit": 10}, section="whatsapp",
        ),
        # Health is volatile for a different reason than rail/home: it is
        # push-fed by the phone on its own schedule, so a cached snapshot may
        # predate the night's data and serve a plausible-looking zero (see the
        # module docstring). The reads are cheap local queries — always live.
        Source(
            "health_summary", "health.query", "summary",
            section="pulse", gated_on="health.query", volatile=True,
        ),
        Source(
            "health_sleep", "health.query", "sleep",
            section="pulse", gated_on="health.query", volatile=True,
        ),
        Source(
            "health_trends", "health.query", "trends", {"days": 7},
            section="pulse", gated_on="health.query", volatile=True,
        ),
        Source(
            "health_workouts", "health.query", "workouts", {"days": 7},
            section="pulse", gated_on="health.query", volatile=True,
        ),

        Source(
            "lastfm_recent", "music.query", "recent", {"limit": 15},
            section="pulse", gated_on="music.query",
        ),
        Source(
            "lastfm_stats", "music.query", "stats", {"period": "this_week"},
            section="pulse", gated_on="music.query",
        ),

        Source(
            "coffee_current", "coffee.query", "current",
            section="coffee", gated_on="coffee.query",
        ),
        Source(
            "coffee_recent_brews", "coffee.query", "recent_brews", {"limit": 7},
            section="coffee", gated_on="coffee.query",
        ),
    ]

    # Transport: weekdays only, and only once a station is configured. The
    # direction filter is a preference because "Northbound" is meaningful
    # only relative to a particular line.
    if not ctx.is_weekend:
        rail_args: dict[str, Any] = {"limit": 5}
        direction = (p.get("rail.direction") or "").strip()
        if direction:
            rail_args["direction"] = direction
        sources.append(
            Source("rail", "rail.query", "departures", rail_args,
                   section="transport", volatile=True)
        )

    # Appliance history is per-home, so it is driven entirely by preference.
    # No entities configured means no laundry/dishwasher lines — there is no
    # sensible default entity id to ship.
    for entity_id in p.get("house.appliance_entities") or []:
        sources.append(
            Source(
                f"appliance:{entity_id}", "homeassistant.entities", "history",
                {"entity_id": entity_id, "days": 2},
                section="house", volatile=True,
            )
        )

    return sources


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    values: dict[str, Any] = field(default_factory=dict)
    stamps: dict[str, float] = field(default_factory=dict)


# Keyed by user_id. Per-user containers rather than one flat dict with
# composite keys: it makes "this user's cache" a thing you can drop whole,
# and makes a cross-user read structurally impossible rather than a matter
# of getting the key format right.
_CACHE: dict[int, _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


def _cache_read(user_id: int, keys: list[str], max_age: float) -> dict[str, Any]:
    """Fresh cached values for `keys`, by whatever subset is still valid."""
    now = time.monotonic()
    out: dict[str, Any] = {}
    with _CACHE_LOCK:
        entry = _CACHE.get(user_id)
        if entry is None:
            return out
        for key in keys:
            stamp = entry.stamps.get(key)
            if stamp is not None and (now - stamp) <= max_age:
                out[key] = entry.values[key]
    return out


def _cache_write(user_id: int, values: dict[str, Any]) -> None:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.setdefault(user_id, _CacheEntry())
        for key, value in values.items():
            entry.values[key] = value
            entry.stamps[key] = now


def clear_cache(user_id: int | None = None) -> None:
    """Drop cached sources — all users, or one. Used by tests and by the
    dashboard when a user's preferences change (a new appliance entity or a
    different mail limit shouldn't wait out the TTL)."""
    with _CACHE_LOCK:
        if user_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(user_id, None)


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


def _decode(raw: Any) -> Any:
    """Facade methods return JSON strings; a few return a bare message.

    An unconfigured integration legitimately returns prose naming the fix
    (irish_rail does this with no station configured), so a decode failure is
    not an error — it's a message worth passing through verbatim.
    """
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"message": raw}


def _fetch_one(source: Source, user_id: int) -> tuple[str, Any]:
    """Run one source in its own session, with the user context re-pinned.

    Both halves matter and both are easy to omit:
      * `use_user` — ContextVars do not cross into pool threads, so without
        this the handler either raises or reads a stale binding.
      * a fresh session — SQLAlchemy sessions are not thread-safe.
    """
    from app.db import get_db

    try:
        with use_user(user_id):
            with get_db().session() as session:
                facade = get_capability(source.capability)
                method = getattr(facade, source.method)
                return source.key, _decode(method(session, dict(source.args)))
    except Exception as e:  # noqa: BLE001
        # One failing source must never take down the brief — a dead Gmail
        # token shouldn't cost you the weather. The template renders
        # "⚠️ unavailable" for exactly this shape.
        logger.warning("daily brief: source %s failed: %s", source.key, e)
        return source.key, {"error": str(e)[:300]}


def _available_sections(session: Session, user_id: int, sources: list[Source]) -> set[str]:
    """Capabilities the user actually has data for, as a set of section names.

    This is the auto-omission half of personalisation, and it needs no
    configuration: a user with no scrobbles gets no Listening lines, a user
    with no brews gets no Coffee section. It reuses each facade's `has_data()`
    — the same mechanism `/api/v1/instructions` uses to decide which
    integrations to describe to a given user (sam-rollout D1).

    Run in one pass on the caller's session before the fan-out, deliberately:
    each check is a single indexed count, and a source cannot be gated on a
    check racing alongside it.
    """
    verdicts: dict[str, bool] = {}
    for source in sources:
        cap = source.gated_on
        if cap is None or cap in verdicts:
            continue
        try:
            facade = get_capability(cap)
            verdicts[cap] = bool(facade.has_data(session, user_id))
        except Exception:  # noqa: BLE001
            # Unknown beats absent: if the check itself breaks, fetch the
            # source and let it report its own emptiness. Silently dropping a
            # section because a count query failed is the worse failure.
            logger.exception("daily brief: has_data check failed for %s", cap)
            verdicts[cap] = True
    return {cap for cap, ok in verdicts.items() if ok}


def build(
    session: Session,
    user_id: int,
    *,
    since: str | None = None,
    sections: list[str] | None = None,
    refresh: bool = False,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    include_volatile: bool = True,
    today: date | None = None,
) -> dict[str, Any]:
    """Compose the brief for one user.

    `sections` filters to a subset — this is what makes `/refresh transport`
    cheap: it fetches the two sources that feed Transport instead of all 23.
    Sources with no section (system alerts) are always included.

    `include_volatile=False` is the pre-warm path: build only the cacheable
    half, since warming a departure board would be pointless.
    """
    today = today or datetime.now(timezone.utc).date()
    prefs = prefs_service.get_all(session, user_id)

    ctx = Ctx(user_id=user_id, prefs=prefs, since=since, today=today)
    all_sources = build_sources(ctx)

    # A section the user has switched off is never fetched at all. Sources
    # with section=None bypass this — alerts are infrastructure, not content.
    enabled = set(prefs.get("daily_note.sections") or prefs_service.DEFAULT_SECTIONS)
    if sections is not None:
        requested = {s.strip().lower() for s in sections if s.strip()}
        enabled &= requested

    have_data = _available_sections(session, user_id, all_sources)

    selected: list[Source] = []
    skipped_no_data: list[str] = []
    for source in all_sources:
        if source.section is not None and source.section not in enabled:
            continue
        if source.gated_on is not None and source.gated_on not in have_data:
            skipped_no_data.append(source.key)
            continue
        if source.volatile and not include_volatile:
            continue
        selected.append(source)

    cacheable = [s for s in selected if not s.volatile]
    cached: dict[str, Any] = {}
    if not refresh and cacheable:
        cached = _cache_read(user_id, [s.key for s in cacheable], max_age_seconds)

    to_fetch = [s for s in selected if s.key not in cached]

    fetched: dict[str, Any] = {}
    if to_fetch:
        workers = min(MAX_WORKERS, len(to_fetch))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, value in pool.map(lambda s: _fetch_one(s, user_id), to_fetch):
                fetched[key] = value

    # Only non-volatile results are written back. A volatile source that
    # slipped into the cache would be served stale on the next call, which is
    # the whole failure this design is avoiding.
    writable = {
        s.key: fetched[s.key]
        for s in to_fetch
        if not s.volatile and s.key in fetched and "error" not in _as_dict(fetched[s.key])
    }
    if writable:
        _cache_write(user_id, writable)

    payload: dict[str, Any] = {**cached, **fetched}
    payload["_meta"] = {
        "user_id": user_id,
        "date": today.isoformat(),
        "lookback_start": ctx.lookback_start.isoformat(),
        "is_weekend": ctx.is_weekend,
        "sections_enabled": sorted(enabled),
        "sources_fetched": sorted(s.key for s in to_fetch),
        "sources_from_cache": sorted(cached),
        "sources_skipped_no_data": sorted(skipped_no_data),
        "preferences": prefs,
        # The one thing the caller still has to do itself, and why.
        "note": (
            "Read-only. snag_capture is NOT included here because it writes; "
            "call it separately if the Snags section is wanted."
        ),
    }
    return payload


def _as_dict(value: Any) -> dict:
    """Best-effort dict view, so the error check above never raises."""
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Pre-warm
# ---------------------------------------------------------------------------


def prewarm_blocking() -> int:
    """Build the cacheable half of the brief for every active user.

    Returns the number of users warmed. Driven by a cron declared in this
    package's manifest — see `background_tasks` there for the schedule and
    why it runs through the morning rather than once.

    Deliberately skips volatile sources: warming a departure board caches
    something that must never be served from cache anyway.
    """
    from app.db import get_db
    from app.models.users import User

    warmed = 0
    with get_db().session() as session:
        user_ids = [row[0] for row in session.query(User.id).all()]

    for user_id in user_ids:
        try:
            with get_db().session() as session:
                build(
                    session,
                    user_id,
                    include_volatile=False,
                    refresh=True,          # rebuild rather than re-read
                    max_age_seconds=0,
                )
            warmed += 1
        except Exception:  # noqa: BLE001
            # A pre-warm failure is invisible to the user (the live call just
            # fetches normally), so log and carry on to the next user rather
            # than aborting the sweep.
            logger.exception("daily brief pre-warm failed for user %s", user_id)

    logger.info("daily brief pre-warm: %d/%d users", warmed, len(user_ids))
    return warmed


async def run_prewarm() -> None:
    """Async entrypoint for the manifest's `background_tasks` TaskSpec."""
    import asyncio

    try:
        await asyncio.to_thread(prewarm_blocking)
    except Exception:  # noqa: BLE001
        logger.exception("daily brief pre-warm task failed")
