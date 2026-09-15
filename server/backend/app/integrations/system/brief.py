"""The daily brief — every read-only source the morning note needs, in one call.

## Why this exists

`/daily-note` step 4 used to enumerate 24 separate MCP tool calls. That cost
~12s, and almost none of it was query time: each call is its own HTTPS
round-trip (a WireGuard hop, since `.mcp.json` points at the tailnet URL),
re-validating a bearer, opening a session, going through `app.plugin.dispatch`
and writing a `runs` audit row (kind="tool_call"). Fanning in collapses 23 round-trips into
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

## Comms are windowed, not just capped (2026-09-04)

`comms.mail_limit`/`comms.whatsapp_limit` used to be the primary filter —
"newest N messages" — with the result that a single busy morning could
consume the whole budget and make a multi-day lookback window (the weekend,
on a Monday) invisible while `_meta.lookback_start` sat there, already
correctly computed, unused for this. `mail_recent` and `whatsapp` now fetch
a pool bounded by `_comms_pool_limit()` (bigger than the display cap) filtered
by `lookback_start`, then `_slice_window`/`_breadth_select` apply the cap
**breadth-first across the days present** rather than taking the newest N —
see `_breadth_select`'s docstring for why day-round-robin was chosen over a
plain per-chat cap. The result carries `messages`, `window_requested`,
`window_covered` (oldest/newest of what's actually returned) and `truncated`
(+ `dropped_count`) so a short window is reported, never silently presented
as complete. This is deliberately separate from `data_freshness`, which
answers "is data still arriving" — not "how much history did we return".
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

# How much bigger a comms pool fetch is than the display cap, and the hard
# ceiling on it regardless of the cap. A single query still returns "newest
# N", so to apply the cap breadth-first (see `_slice_window`) we first have
# to see more than N messages in the window. 4x is enough to cover a busy
# Monday's 4-day lookback without one long thread crowding everything else
# out of the pool itself; the ceiling keeps a very high per-user cap from
# turning into an unbounded scan.
# Raised 100->600 2026-09-07 alongside the display-cap defaults above — a
# ceiling sized for the old 25/30 defaults would have clamped the new 40/120
# defaults' pool fetch back down before `_slice_window` ever got to apply its
# breadth-first cap, silently undoing the point of raising them.
_COMMS_POOL_MULTIPLIER = 4
_COMMS_POOL_CEILING = 600


def _comms_pool_limit(cap: int) -> int:
    return min(_COMMS_POOL_CEILING, max(cap * _COMMS_POOL_MULTIPLIER, cap))


# ---------------------------------------------------------------------------
# Section vocabulary
# ---------------------------------------------------------------------------

# There are two vocabularies for "section", and callers reasonably use either:
#
# - the *preference* vocabulary (`daily_note.sections` / `Source(section=...)`
#   in `build_sources` below) — pulse, food, coffee, transport, house, snags,
#   today, tasks, email, whatsapp, meetings, notes.
# - the *rendered* vocabulary (`brief_render.SECTIONS` and both prompt
#   templates) — alerts, pulse, coffee, transport, consumables, snags,
#   calendar, listening, freshness.
#
# `sections=[...]` gates which *sources* get fetched, which only understands
# the preference vocabulary. A caller passing the rendered name (e.g.
# `/kickoff` asking for `["calendar"]`) previously matched nothing: `enabled`
# came back empty, the calendar source was never fetched, and `rendered`
# reported "not measured" for a day with a full calendar. This map lets
# either vocabulary select the right sources.
#
# `alerts` and `freshness` don't gate anything — alerts are always fetched
# (section=None on that Source) and freshness is derived from the alerts
# payload rather than its own source — so they map to `None`: accepted as
# known names, but contribute nothing to the `enabled` set.
SECTION_ALIASES: dict[str, str | None] = {
    "calendar": "today",
    "consumables": "house",
    # The lastfm sources are tagged section="pulse" (see `build_sources`) —
    # `render_pulse` never reads them, `render_listening` does, so "listening"
    # is a real independent *rendered* section fed by a *preference* section
    # named "pulse". Retagging the sources to a new "listening" preference
    # section would separate them from `daily_note.show_listening`'s sibling
    # gate with no benefit, so the alias is the smaller correct change.
    "listening": "pulse",
    "alerts": None,
    "freshness": None,
}


def _normalise_requested_sections(
    requested: set[str],
) -> tuple[set[str], list[str]]:
    """Map `requested` (either vocabulary) onto preference-section names.

    Returns `(preference_sections, unknown)` — `unknown` is every requested
    name that is neither a preference section nor a known alias, so a typo
    is loud (`_meta.sections_unknown`) instead of silently fetching nothing.
    """
    known_preference_sections = set(prefs_service.DEFAULT_SECTIONS)
    resolved: set[str] = set()
    unknown: list[str] = []
    for name in requested:
        if name in known_preference_sections:
            resolved.add(name)
        elif name in SECTION_ALIASES:
            target = SECTION_ALIASES[name]
            if target is not None:
                resolved.add(target)
        else:
            unknown.append(name)
    return resolved, sorted(unknown)


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

    @property
    def comms_window(self) -> tuple[str, str]:
        """(after_iso, window_source) for `mail_recent`/`whatsapp`'s `after`.

        `since` — the previous daily note's timestamp, when the caller has
        one — narrows the comms window whenever it's later (more recent)
        than the default `lookback_start`: a note written at 09:00 today
        makes "since this morning's note" a tighter, more useful window than
        "since Friday". `lookback_start` stays the floor for everyone else
        (no `since`, or a `since` that predates it — e.g. a stale timestamp
        from a very old note) — never widened past what `lookback_start`
        already computed for today's weekday.

        `since` is a raw ISO timestamp (usually carrying a time-of-day);
        `lookback_start` is a bare date. Comparing them as ISO strings
        against `lookback_start`'s midnight is exact for "is `since` later"
        and needs no reparsing to a datetime — ISO 8601 date/datetime strings
        of this shape sort lexically the same as they compare temporally.
        """
        lookback_iso = self.lookback_start.isoformat()
        if self.since and self.since > lookback_iso:
            return self.since, "since"
        return lookback_iso, "lookback"


@dataclass(frozen=True)
class Source:
    """One composed source in the brief."""

    key: str                   # where it lands in the payload
    capability: str            # resolved via get_capability()
    method: str                # facade method name
    args: dict[str, Any] = field(default_factory=dict)
    # Section this source feeds. None means "always fetch" (system alerts).
    # Used both for `daily_note.sections` and for /checkin-style filtering.
    section: str | None = None
    # Never cached, never pre-warmed — see the module docstring.
    volatile: bool = False
    # Capability whose facade exposes has_data(); when set and the user has
    # no data, the source is skipped entirely rather than fetched and thrown
    # away. This is what makes a section auto-omit for a user who doesn't
    # use that integration, with no configuration required.
    gated_on: str | None = None
    # Set for comms sources (mail_recent, whatsapp) whose count is a safety
    # cap on a *time window*, not the primary filter — see `_slice_window`'s
    # docstring. When set, the raw fetch pulls a larger pool (bounded by
    # `_comms_pool_limit`) so the cap can be applied breadth-first across the
    # window rather than by the underlying query's own "newest N" ordering.
    comms_cap: int | None = None


def build_sources(ctx: Ctx) -> list[Source]:
    """The source list for this user, on this day, with their preferences."""
    p = ctx.prefs
    # Fallbacks mirror `preferences.PREFERENCES`; the registry is the source.
    # Raised 2026-09-07 (25->40, 30->120): Alex — "most WhatsApp messages are
    # short", so the character cost per message is lower than the original
    # 50/50 sizing assumed. `_COMMS_POOL_CEILING` moved with it.
    mail_limit = max(1, int(p.get("comms.mail_limit", 40)))
    wa_limit = max(1, int(p.get("comms.whatsapp_limit", 120)))
    comms_after, _window_source = ctx.comms_window

    sources: list[Source] = [
        # Always fetched: if an integration is degraded, the caller needs to
        # know the rest of this payload may be stale before trusting it.
        Source("alerts", "system.alerts", "alerts", volatile=True),

        # Monitoring alert log (lios#230) — what fired/cleared since the
        # previous note, read by `render_alerts`'s "Monitoring since last
        # note" sub-block. Always fetched (section=None), same reasoning as
        # `alerts` above: this is infrastructure, not opt-in content, and a
        # kickoff needs to know about it whether or not the caller asked
        # for it by name. Volatile: the whole point is that it must never
        # show a stale window.
        Source(
            "alert_log", "alerts.query", "events_since",
            {"since": ctx.since or ctx.lookback_start.isoformat()},
            volatile=True,
        ),

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
        # mail_recent and whatsapp are windowed, not just capped by count —
        # see `_slice_window`'s docstring for why `lookback_start` is the
        # primary filter and the count is only a safety ceiling. The `limit`
        # sent here is a *pool* size (bigger than the display cap) so the
        # cap can be applied breadth-first afterwards instead of the
        # underlying query's own "newest N".
        Source(
            "mail_recent", "mail.query", "recent",
            {"after": comms_after, "limit": _comms_pool_limit(mail_limit)},
            section="email", gated_on="mail.query", comms_cap=mail_limit,
        ),
        Source(
            "whatsapp", "whatsapp.query", "recent",
            {"after": comms_after, "limit": _comms_pool_limit(wa_limit)},
            section="whatsapp", gated_on="whatsapp.query", comms_cap=wa_limit,
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
        # Deliberately NOT gated_on="strava.query": `has_data()` answers "has
        # this user EVER stored an activity", and gating on it would drop
        # this key entirely for a connected-but-quiet week — exactly the
        # "went silent" case #195 needs `render_pulse` to be able to name
        # rather than have it look identical to "not connected". Always
        # fetched; the facade itself distinguishes not-connected from
        # connected-with-nothing-this-week. Cheap regardless (one OAuthToken
        # row lookup when there's nothing to report).
        Source(
            "strava_activities", "strava.query", "activities", {"days": 7},
            section="pulse", volatile=True,
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


def _breadth_select(messages: list[dict], cap: int) -> list[dict]:
    """Pick `cap` messages out of a larger pool, spread across the days
    present rather than just the newest `cap`.

    This is the fix for the backlog item's actual failure: a single long
    conversation near "now" consumed the whole count budget, so the rest of
    a multi-day lookback window — a full weekend, in the case that was
    measured — was invisible even though it was inside the window that was
    fetched. Round-robining by calendar day means every day in the pool gets
    at least one pick before any day gets a second, so a chatty Monday
    morning cannot crowd out a quiet Saturday.

    `messages` is assumed newest-first (the underlying `ListTool`'s default
    order), each carrying a `date` ISO string — true of both `whatsapp` and
    `mail_recent`'s `to_dict`. Within a day, the newest-first order is kept
    (the first pick from a day is that day's most recent message), and the
    final list is re-sorted chronologically for readability.
    """
    if cap <= 0:
        return []
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for m in messages:
        day = (m.get("date") or "")[:10]
        if day not in buckets:
            buckets[day] = []
            order.append(day)
        buckets[day].append(m)
    order.sort()  # oldest day first, so the round-robin favours it

    selected: list[dict] = []
    cursors = dict.fromkeys(order, 0)
    while len(selected) < cap:
        progressed = False
        for day in order:
            if len(selected) >= cap:
                break
            i = cursors[day]
            items = buckets[day]
            if i < len(items):
                selected.append(items[i])
                cursors[day] = i + 1
                progressed = True
        if not progressed:
            break

    selected.sort(key=lambda m: m.get("date") or "")
    return selected


def _slice_window(messages: list[dict], cap: int, window_requested: str | None) -> dict:
    """Cap a comms pool to `cap` while reporting the window actually covered.

    `_meta.lookback_start` is already the correct filter; the count is only
    a safety ceiling (see the module's originating backlog item). When the
    pool fits under the cap, nothing is dropped and `window_covered` spans
    the whole pool. When it doesn't, `_breadth_select` trims it — day-by-day,
    not newest-first — and `truncated`/`dropped_count` say so honestly
    rather than letting a short window read as full coverage.
    """
    if len(messages) <= cap:
        selected = messages
        truncated = False
        dropped = 0
    else:
        selected = _breadth_select(messages, cap)
        truncated = True
        dropped = len(messages) - len(selected)

    dates = [m.get("date") for m in selected if m.get("date")]
    return {
        "messages": selected,
        "window_requested": window_requested,
        "window_covered": {
            "oldest": min(dates) if dates else None,
            "newest": max(dates) if dates else None,
        },
        "truncated": truncated,
        "dropped_count": dropped,
    }


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
                decoded = _decode(method(session, dict(source.args)))
                if source.comms_cap is not None and isinstance(decoded, list):
                    decoded = _slice_window(
                        decoded, source.comms_cap, source.args.get("after")
                    )
                return source.key, decoded
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
    render: bool = False,
) -> dict[str, Any]:
    """Compose the brief for one user.

    `sections` filters to a subset — this is what makes `/checkin transport`
    cheap: it fetches the two sources that feed Transport instead of all 23.
    Sources with no section (system alerts) are always included.

    `include_volatile=False` is the pre-warm path: build only the cacheable
    half, since warming a departure board would be pointless.

    `render=True` adds a top-level `rendered: {<section>: <markdown>}` built
    by `app.integrations.system.brief_render` over the payload assembled
    below — see that module for the section list, headings and the
    absent/empty/error rules. It never changes what's fetched; it only
    formats what's already there.
    """
    today = today or datetime.now(timezone.utc).date()
    prefs = prefs_service.get_all(session, user_id)

    ctx = Ctx(user_id=user_id, prefs=prefs, since=since, today=today)
    all_sources = build_sources(ctx)

    # A section the user has switched off is never fetched at all. Sources
    # with section=None bypass this — alerts are infrastructure, not content.
    enabled = set(prefs.get("daily_note.sections") or prefs_service.DEFAULT_SECTIONS)
    sections_unknown: list[str] = []
    if sections is not None:
        requested = {s.strip().lower() for s in sections if s.strip()}
        # `requested` may use either the preference vocabulary (this
        # function's `enabled` set) or the rendered vocabulary (`calendar`,
        # `consumables`, `listening`, ...) — see `SECTION_ALIASES`.
        normalised, sections_unknown = _normalise_requested_sections(requested)
        enabled &= normalised

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

    comms_after, window_source = ctx.comms_window
    payload: dict[str, Any] = {**cached, **fetched}
    payload["_meta"] = {
        "user_id": user_id,
        "date": today.isoformat(),
        "lookback_start": ctx.lookback_start.isoformat(),
        "is_weekend": ctx.is_weekend,
        "sections_enabled": sorted(enabled),
        "sections_unknown": sections_unknown,
        "sources_fetched": sorted(s.key for s in to_fetch),
        "sources_from_cache": sorted(cached),
        "sources_skipped_no_data": sorted(skipped_no_data),
        "preferences": prefs,
        # Comms window actually used for mail_recent/whatsapp's `after`, and
        # why: "since" when a caller-supplied `since` narrowed it past the
        # weekday default, "lookback" otherwise. See `Ctx.comms_window`.
        "comms_window_start": comms_after,
        "window_source": window_source,
        # The one thing the caller still has to do itself, and why.
        "note": (
            "Read-only. snag_capture is NOT included here because it writes; "
            "call it separately if the Snags section is wanted."
        ),
    }
    if render:
        payload["rendered"] = _build_rendered(
            payload, user_id, refresh=refresh, max_age_seconds=max_age_seconds,
        )
    return payload


def _build_rendered(
    payload: dict[str, Any], user_id: int, *, refresh: bool, max_age_seconds: float,
) -> dict[str, str]:
    """The `rendered: {<section>: <markdown>}` block for `render=True`.

    Reuses the same per-user cache `_cache_read`/`_cache_write` use for raw
    sources, under `rendered:<section>` keys so they can't collide with a
    raw source key of the same name. Only the sections in
    `brief_render.NEVER_CACHE_RENDERED` are always recomputed fresh; the rest
    are served warm when the cache is fresh and `refresh` wasn't requested,
    same TTL as everything else in this module.
    """
    from app.integrations.system import brief_render

    cache_keys = {s: f"rendered:{s}" for s in brief_render.SECTIONS}
    cacheable = [s for s in brief_render.SECTIONS if s not in brief_render.NEVER_CACHE_RENDERED]

    cached_rendered: dict[str, str] = {}
    if not refresh and cacheable:
        raw = _cache_read(user_id, [cache_keys[s] for s in cacheable], max_age_seconds)
        cached_rendered = {s: raw[cache_keys[s]] for s in cacheable if cache_keys[s] in raw}

    to_render = tuple(s for s in brief_render.SECTIONS if s not in cached_rendered)
    computed = brief_render.render_all(payload, to_render) if to_render else {}

    rendered: dict[str, str] = {**cached_rendered, **computed}

    writable = {
        cache_keys[s]: computed[s]
        for s in to_render
        if s not in brief_render.NEVER_CACHE_RENDERED
    }
    if writable:
        _cache_write(user_id, writable)

    return rendered


def _as_dict(value: Any) -> dict:
    """Best-effort dict view, so the error check above never raises."""
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Pre-warm
# ---------------------------------------------------------------------------


def prewarm_blocking() -> int:
    """Build the cacheable half of the brief for every active user.

    Returns the number of users warmed. Driven by two crons declared in this
    package's manifest (a 15-minute morning cadence plus an hourly one that
    runs all day, added 2026-09-07) — see `background_tasks` there for the
    schedule and why it runs repeatedly rather than once.

    Deliberately skips volatile sources: warming a departure board caches
    something that must never be served from cache anyway. `render=True`
    additionally renders and caches the markdown fragments that don't depend
    on a volatile source (Coffee, Snags, Listening — see
    `brief_render.NEVER_CACHE_RENDERED`), so a `render=True` call on a warm
    cache only has to compute the handful of sections that always recompute
    fresh, rather than the whole set.
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
                    render=True,
                )
            warmed += 1
        except Exception:  # noqa: BLE001
            # A pre-warm failure is invisible to the user (the live call just
            # fetches normally), so log and carry on to the next user rather
            # than aborting the sweep.
            logger.exception("daily brief pre-warm failed for user %s", user_id)

    logger.info("daily brief pre-warm: %d/%d users", warmed, len(user_ids))

    # S5.1: report what this run touched onto the enclosing `runs` ledger
    # row, if there is one (there always is in production — `run_prewarm`
    # is only ever invoked through `app.scheduler`'s generic wrap; a direct
    # unit-test call has no enclosing record_run and current_run() is a
    # no-op None then). Does not change this function's return value or
    # control flow.
    from app.services.runs import current_run

    run = current_run()
    if run is not None:
        run.touched(users_warmed=warmed, users_total=len(user_ids))

    return warmed


async def run_prewarm() -> None:
    """Async entrypoint for the manifest's `background_tasks` TaskSpec."""
    import asyncio

    try:
        await asyncio.to_thread(prewarm_blocking)
    except Exception:  # noqa: BLE001
        logger.exception("daily brief pre-warm task failed")
