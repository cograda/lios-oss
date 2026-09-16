"""Shared helpers for MCP tool handlers.

These are the blessed implementations that replace mechanical, copy-pasted
duplication across `app/integrations/*/tools.py`: per-user query scoping,
row-to-dict serialization, semantic-search enrich callbacks, and stats
primitives (count-by-period, top-N group-by).

Bespoke logic — joins across models, live external-API calls, admin
operations, anything with real domain shape — stays hand-written in each
integration's tools.py. These helpers only exist to delete the mechanical
1:1 boilerplate; they are deliberately not a framework.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import or_
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Query, Session

from app.auth.context import current_user_id


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


def scoped_query(session: Session, model: type) -> Query:
    """THE blessed query entry point for per-user-owned models.

    Starts `session.query(model)` and, if `model` carries any of its
    per-user column(s) (`app.privacy.user_columns_for` — `("user_id",)` for
    a plain `UserOwnedMixin` model, or a table-specific tuple such as
    `vault_read_grants`'s `("grantee_user_id", "owner_user_id")`), filters
    to rows where the requesting user matches ANY of them. A table with
    none of those columns (household-shared) comes back unscoped.

    The OR-across-columns shape is deliberate, not an accident of iterating
    a tuple: `vault_read_grants` needs a row visible to BOTH the grantee and
    the owner, which is not "the row has one owner" — a single-column
    scoping rule cannot express it.

    `app/tools/list_tool.py` and `app/tools/search_tool.py` route their
    auto-scoping through this same function, so there is exactly one
    scoping implementation across the DSL and hand-written handlers.
    """
    from app.privacy import user_columns_for

    query = session.query(model)
    cols = [name for name in user_columns_for(model) if hasattr(model, name)]
    if cols:
        uid = current_user_id()
        query = query.filter(or_(*(getattr(model, name) == uid for name in cols)))
    return query


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def iso_or_none(value: Any) -> str | None:
    """Common transform for `serialize()`: datetime/date -> ISO string, else None."""
    return value.isoformat() if value else None


def serialize(
    row: Any,
    fields: Iterable[str],
    renames: dict[str, str] | None = None,
    transforms: dict[str, Callable[[Any], Any]] | None = None,
) -> dict[str, Any]:
    """Mechanical row -> dict serialization, replacing the `_x_to_dict` families.

    `fields` are attribute names read off `row` in order. `renames` maps an
    attribute name to a different output key (default: same name).
    `transforms` maps an attribute name to a callable applied to the raw
    value before output (e.g. `iso_or_none` for timestamp columns).

    Bespoke serializers — computed fields, cross-model joins, live-API
    response shapes — stay hand-written; this only replaces the 1:1
    mechanical mapping.
    """
    renames = renames or {}
    transforms = transforms or {}
    out: dict[str, Any] = {}
    for field in fields:
        value = getattr(row, field)
        if field in transforms:
            value = transforms[field](value)
        out[renames.get(field, field)] = value
    return out


# ---------------------------------------------------------------------------
# Semantic-search enrich factory
# ---------------------------------------------------------------------------


def make_enrich(
    *,
    model: type | None = None,
    id_column: str | None = None,
    fields: Iterable[str] = (),
    renames: dict[str, str] | None = None,
    transforms: dict[str, Callable[[Any], Any]] | None = None,
    id_key: str = "id",
    metadata_fields: Iterable[str] = (),
    preview_len: int = 300,
    preview_key: str = "preview",
) -> Callable[[Session, list[dict]], list[dict]]:
    """Factory for `SemanticSearchTool` `enrich` callbacks.

    Every hand-written enrich callback did the same two things, in some
    combination: (a) batch-fetch DB rows matching the raw hits' `source_id`
    and copy a handful of fields across (google_mail's `_semantic_enrich`),
    and/or (b) pull fields out of the JSON `metadata` blob stashed on the
    embedding row at enqueue time, with no DB round-trip at all (whatsapp's
    `_semantic_enrich`). This factory covers both, composably:

    - Pass `model` + `id_column` to batch-fetch and `serialize()` `fields`
      (optionally `renames`/`transforms`) from matching rows.
    - Pass `metadata_fields` to copy keys straight out of each hit's
      `metadata` JSON with no query at all.
    - `id_key` names the output key that carries the raw `source_id`
      (google_mail uses "id", whatsapp uses "segment_id").

    Every result always carries `score`, `id_key`, and a `preview_key`
    truncated to `preview_len` characters of the raw hit's preview text.
    """
    renames = renames or {}
    transforms = transforms or {}

    def enrich(session: Session, raw_results: list[dict]) -> list[dict]:
        model_rows: dict[Any, Any] = {}
        if model is not None and id_column is not None:
            ids = [r["source_id"] for r in raw_results]
            rows = (
                scoped_query(session, model)
                .filter(getattr(model, id_column).in_(ids))
                .all()
            )
            model_rows = {getattr(row, id_column): row for row in rows}

        out: list[dict[str, Any]] = []
        for r in raw_results:
            item: dict[str, Any] = {"score": r["score"], id_key: r["source_id"]}

            if model is not None:
                row = model_rows.get(r["source_id"])
                if row is not None:
                    item.update(serialize(row, fields, renames=renames, transforms=transforms))
                else:
                    item.update({renames.get(f, f): None for f in fields})

            if metadata_fields:
                meta = json.loads(r["metadata"]) if r.get("metadata") else {}
                for f in metadata_fields:
                    item[f] = meta.get(f)

            item[preview_key] = r["preview"][:preview_len]
            out.append(item)

        return out

    return enrich


# ---------------------------------------------------------------------------
# Timestamp / age
# ---------------------------------------------------------------------------


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Normalise a datetime to tz-aware UTC.

    Replaces the copy-pasted "attach tzinfo if naive, then diff against
    now-UTC" dance seen in commute/tools.py, system/tools.py's morning
    briefing, and elsewhere. A naive `dt` is assumed to already be UTC (true
    for every table in this codebase that stores naive timestamps at all —
    verify per call site before routing through this; Dublin-local naive
    values, e.g. commute's solver-internal fields, must NOT go through here)
    and gets UTC attached; an aware `dt` is converted to UTC. `None` passes
    through as `None`.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_seconds(dt: datetime | None, *, now: datetime | None = None) -> float | None:
    """Seconds elapsed between `dt` and `now` (default: current UTC time).

    `dt` is normalised via `ensure_utc()` first, so naive values are treated
    as UTC. Returns `None` if `dt` is `None`.
    """
    normalised = ensure_utc(dt)
    if normalised is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - normalised).total_seconds()


# ---------------------------------------------------------------------------
# Stats primitives
# ---------------------------------------------------------------------------

_CALENDAR_PERIODS = {"this_week", "this_month", "this_year"}
_ROLLING_PERIODS = {
    "7day": 7,
    "1month": 30,
    "3month": 90,
    "6month": 180,
    "12month": 365,
}


def period_since(period: str, now: datetime | None = None) -> datetime | None:
    """Resolve a period keyword to a start datetime, or None for all-time.

    Supports calendar-aligned periods (`this_week`, `this_month`,
    `this_year`) and Last.fm-style rolling windows (`7day`, `1month`,
    `3month`, `6month`, `12month`). `all_time`/`overall` (or anything
    unrecognised) resolve to None, meaning "no lower bound".
    """
    now = now or datetime.now(timezone.utc)

    if period == "this_week":
        start = now - timedelta(days=now.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "this_month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "this_year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if period in _ROLLING_PERIODS:
        return now - timedelta(days=_ROLLING_PERIODS[period])
    return None


def top_n_group_by(
    query: Query,
    group_col: Any,
    limit: int,
    count_col: Any | None = None,
    label: str = "n",
) -> list[tuple]:
    """Group `query` by `group_col`, count rows, order desc, limit N.

    Replaces the copy-pasted `.with_entities(col, func.count(...)).group_by(col)
    .order_by(func.count(...).desc()).limit(n).all()` shape used by lastfm's
    top artists/tracks, coffee's origin/process/roaster/method breakdowns,
    etc. Returns raw `(value, count)` row tuples — callers format them.
    """
    count_expr = sa_func.count(count_col if count_col is not None else group_col)
    return (
        query.with_entities(group_col, count_expr.label(label))
        .group_by(group_col)
        .order_by(count_expr.desc())
        .limit(limit)
        .all()
    )


def handler_for(tools: Iterable[dict], name: str) -> Callable[[Session, dict], str]:
    """Return the handler callable of a built tool dict, by tool name.

    Hand-written handlers (`handle_foo`) are module-level functions a facade
    can just import. DSL-built ones are not: `ListTool(...).build()` closes
    over the builder's config and stores the result under `"handler"`, so the
    built dict is the *only* place that callable exists. A facade wrapping a
    DSL tool therefore looks it up from its own package's `get_mcp_tools()`
    rather than importing a function that was never defined.

    Resolving by name (not list position) matters — tool order in
    `get_mcp_tools()` is presentational and has been reordered before; an
    index would silently return the wrong tool instead of failing.
    """
    for tool in tools:
        if tool.get("name") == name:
            return tool["handler"]
    raise KeyError(f"No tool named {name!r} in the supplied tool list")
