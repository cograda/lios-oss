"""Per-tool freshness hints for HTTP caching on the REST projection.

Maps a read-only tool name to how many seconds its response may be served
from a private cache before it's stale. Only meaningful for tools annotated
`readOnlyHint: true` (see `app/plugin/dispatch.py`'s use of
`get_tool_annotations` for the same annotation) — a write tool is never
cached regardless of what's in this table.

0 (the default for any tool not listed here) means no-store: a new read-only
tool starts uncacheable, and caching is opted into deliberately per tool
rather than assumed safe. This is a small hand-maintained table rather than
a field on `ToolAnnotations` for now — see the design note's §4.2 for why
("Add the hint as an optional key on tool annotations or a small registry");
promote entries here onto the dataclass if the list grows past a page.
"""

from __future__ import annotations

TOOL_FRESHNESS_SECONDS: dict[str, int] = {
    # Weather changes slowly and syncs on its own */30 schedule (see
    # `integrations/weather/manifest.py`) — 10 minutes is safely inside that.
    "weather_current": 600,
    "weather_forecast": 600,
    # Calendar can change from another device at any time; a minute of
    # staleness is an acceptable trade against hitting the DB on every
    # screen refresh (this is the kickoff/loops "one round trip" case).
    "calendar_today": 60,
    "calendar_list_events": 60,
    "calendar_next_events": 60,
    # Tasks are read back immediately after being written (kickoff, loops,
    # /tunetasks) and must never serve a stale page. Listed explicitly at 0
    # rather than left unlisted, so the choice reads as deliberate.
    "tasks_query": 0,
}


def freshness_seconds(tool_name: str) -> int:
    """Max-age in seconds for a cacheable (read-only) tool's response.

    0 means no-store. Unlisted tools default to 0.
    """
    return TOOL_FRESHNESS_SECONDS.get(tool_name, 0)
