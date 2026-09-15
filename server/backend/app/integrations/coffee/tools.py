"""MCP tool definitions for coffee integration."""

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.coffee.models import Coffee, CoffeeBrew, CoffeeEquipmentProfile
from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike
from app.tools import CustomTool, ListTool, SearchTool, StatsTool, ToolAnnotations
from app.tools.helpers import iso_or_none, scoped_query, serialize, top_n_group_by

logger = logging.getLogger(__name__)


EMBEDDING_SOURCE = "coffee"


def _coffee_embed_text(c: Coffee) -> str:
    """Compose the text we embed for semantic similarity."""
    parts = [
        c.name,
        c.roaster or "",
        c.origin_country or "",
        c.region_farm or "",
        c.process or "",
        c.fermentation or "",
        c.variety or "",
        c.category or "",
        c.roaster_tasting_notes or "",
        c.notes or "",
    ]
    return " | ".join(p for p in parts if p)


def _enqueue_coffee_embedding(session: Session, c: Coffee) -> bool:
    """Queue a coffee for embedding. Safe to call repeatedly — dedupes by hash."""
    from app.services.embedding import EmbeddingService
    text = _coffee_embed_text(c)
    if not text.strip():
        return False
    return EmbeddingService.enqueue(
        session,
        source=EMBEDDING_SOURCE,
        source_id=str(c.id),
        content=text,
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _decimal_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None


_COFFEE_FIELDS = [
    "id", "name", "roaster", "origin_country", "region_farm", "process",
    "fermentation", "variety", "altitude_masl", "category", "weight_g",
    "price", "roaster_tasting_notes", "notes", "rating", "status",
    "purchase_date", "roast_date",
]
_COFFEE_TRANSFORMS = {
    "weight_g": _decimal_or_none,
    "price": _decimal_or_none,
    "purchase_date": iso_or_none,
    "roast_date": iso_or_none,
}


def _coffee_to_dict(c: Coffee) -> dict:
    return serialize(c, _COFFEE_FIELDS, transforms=_COFFEE_TRANSFORMS)


_BREW_FIELDS = [
    "id", "coffee_id", "method", "brew_context", "brewed_at",
    "overall", "extraction_assessment", "flavour_notes", "notes",
]
_BREW_TRANSFORMS = {"brewed_at": iso_or_none}


def _brew_to_dict(b: CoffeeBrew) -> dict:
    out = serialize(b, _BREW_FIELDS, transforms=_BREW_TRANSFORMS)
    if b.brew_context == "cafe":
        out["cafe_name"] = b.cafe_name
        out["drink_type"] = b.drink_type
    else:
        out["dose_g"] = float(b.dose_g) if b.dose_g is not None else None
        out["grind_setting"] = b.grind_setting
        out["water_temp_c"] = float(b.water_temp_c) if b.water_temp_c is not None else None
        if b.method == "espresso":
            out["yield_g"] = float(b.yield_g) if b.yield_g is not None else None
            out["time_s"] = b.time_s
            out["ratio"] = float(b.ratio) if b.ratio is not None else None
        else:  # filter
            out["water_g"] = float(b.water_g) if b.water_g is not None else None
            out["brew_time_s"] = b.brew_time_s
            out["filter_ratio"] = float(b.filter_ratio) if b.filter_ratio is not None else None
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _resolve_coffee(session: Session, arguments: dict) -> Coffee | None:
    """Resolve a coffee by id, or by fuzzy name+roaster match."""
    if (cid := arguments.get("coffee_id")) is not None:
        return session.get(Coffee, int(cid))
    name = (arguments.get("coffee_name") or "").strip()
    roaster = (arguments.get("roaster") or "").strip()
    if not name:
        return None
    q = session.query(Coffee).filter(
        Coffee.name.ilike(f"%{escape_ilike(name)}%", escape=ILIKE_ESCAPE_CHAR)
    )
    if roaster:
        q = q.filter(
            Coffee.roaster.ilike(f"%{escape_ilike(roaster)}%", escape=ILIKE_ESCAPE_CHAR)
        )
    return q.order_by(Coffee.created_at.desc()).first()


# ---------------------------------------------------------------------------
# Stats compute
# ---------------------------------------------------------------------------

def _compute_stats(session: Session, arguments: dict[str, Any]) -> dict:
    period = arguments.get("period", "all_time")
    now = datetime.now(timezone.utc)
    since = None
    if period == "this_week":
        since = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_month":
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_year":
        since = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "30day":
        since = now - timedelta(days=30)
    elif period == "90day":
        since = now - timedelta(days=90)
    elif period == "1year":
        since = now - timedelta(days=365)

    coffees_q = session.query(Coffee)
    brews_q = scoped_query(session, CoffeeBrew)
    if since:
        brews_q = brews_q.filter(CoffeeBrew.brewed_at >= since)
        coffees_q = coffees_q.filter(Coffee.created_at >= since)

    total_coffees = coffees_q.count()
    total_brews = brews_q.count()

    avg_rating = (
        session.query(sa_func.avg(Coffee.rating))
        .filter(Coffee.rating.isnot(None))
        .scalar()
    )

    by_origin = top_n_group_by(
        coffees_q.filter(Coffee.origin_country.isnot(None)),
        Coffee.origin_country, 15, count_col=Coffee.id, label="n",
    )

    by_process = (
        coffees_q.with_entities(
            Coffee.process,
            sa_func.count(Coffee.id).label("n"),
        )
        .filter(Coffee.process.isnot(None))
        .group_by(Coffee.process)
        .order_by(sa_func.count(Coffee.id).desc())
        .all()
    )

    by_roaster = (
        coffees_q.with_entities(
            Coffee.roaster,
            sa_func.count(Coffee.id).label("n"),
            sa_func.avg(Coffee.rating).label("avg_rating"),
        )
        .filter(Coffee.roaster.isnot(None))
        .group_by(Coffee.roaster)
        .order_by(sa_func.count(Coffee.id).desc())
        .limit(15)
        .all()
    )

    by_method = (
        brews_q.with_entities(
            CoffeeBrew.method,
            sa_func.count(CoffeeBrew.id).label("n"),
            sa_func.avg(CoffeeBrew.overall).label("avg_overall"),
        )
        .group_by(CoffeeBrew.method)
        .all()
    )

    return {
        "period": period,
        "total_coffees": total_coffees,
        "total_brews": total_brews,
        "avg_coffee_rating": float(avg_rating) if avg_rating is not None else None,
        "by_origin": [{"origin": o, "count": n} for o, n in by_origin],
        "by_process": [{"process": p, "count": n} for p, n in by_process],
        "by_roaster": [
            {
                "roaster": r,
                "count": n,
                "avg_rating": float(ar) if ar is not None else None,
            }
            for r, n, ar in by_roaster
        ],
        "by_method": [
            {
                "method": m,
                "count": n,
                "avg_overall": float(ao) if ao is not None else None,
            }
            for m, n, ao in by_method
        ],
    }


# ---------------------------------------------------------------------------
# Custom tool handlers
# ---------------------------------------------------------------------------

def handle_log(session: Session, arguments: dict[str, Any]) -> str:
    """Add or upsert a coffee bag."""
    name = (arguments.get("name") or "").strip()
    roaster = (arguments.get("roaster") or "").strip() or None
    if not name:
        return json.dumps({"error": "name is required"})

    existing = (
        session.query(Coffee)
        .filter(Coffee.name == name, Coffee.roaster == roaster)
        .first()
    )
    if existing:
        target = existing
    else:
        target = Coffee(name=name, roaster=roaster)
        session.add(target)

    fields = (
        "origin_country", "region_farm", "process", "fermentation", "variety",
        "altitude_masl", "category", "roaster_tasting_notes", "notes",
        "status", "photo_url",
    )
    for f in fields:
        if f in arguments and arguments[f] is not None:
            setattr(target, f, arguments[f])

    if "weight_g" in arguments:
        target.weight_g = _to_decimal(arguments["weight_g"])
    if "price" in arguments:
        target.price = _to_decimal(arguments["price"])
    if "rating" in arguments:
        target.rating = _to_int(arguments["rating"])
    if "roast_date" in arguments and arguments["roast_date"]:
        target.roast_date = datetime.fromisoformat(arguments["roast_date"]).date()
    if "purchase_date" in arguments and arguments["purchase_date"]:
        target.purchase_date = datetime.fromisoformat(arguments["purchase_date"]).date()

    target.updated_at = datetime.now(timezone.utc)
    session.flush()  # need target.id for embedding enqueue
    _enqueue_coffee_embedding(session, target)
    session.commit()
    return json.dumps({
        "status": "ok",
        "action": "updated" if existing else "created",
        "coffee": _coffee_to_dict(target),
    })


def handle_brew(session: Session, arguments: dict[str, Any]) -> str:
    """Log a brew session."""
    method = arguments.get("method", "espresso")
    if method not in {"espresso", "filter"}:
        return json.dumps({"error": "method must be 'espresso' or 'filter'"})
    brew_context = arguments.get("brew_context", "home")

    coffee = _resolve_coffee(session, arguments) if brew_context == "home" else None
    if brew_context == "home" and not coffee:
        return json.dumps({
            "error": "Couldn't resolve coffee. Pass coffee_id, or coffee_name (+ roaster).",
        })

    brewed_at_str = arguments.get("brewed_at")
    if brewed_at_str:
        brewed_at = datetime.fromisoformat(brewed_at_str)
        if brewed_at.tzinfo is None:
            brewed_at = brewed_at.replace(tzinfo=timezone.utc)
    else:
        brewed_at = datetime.now(timezone.utc)

    dose = _to_decimal(arguments.get("dose_g"))
    yield_g = _to_decimal(arguments.get("yield_g"))
    water_g = _to_decimal(arguments.get("water_g"))
    ratio = None
    filter_ratio = None
    if method == "espresso" and dose and yield_g:
        ratio = (yield_g / dose).quantize(Decimal("0.01"))
    if method == "filter" and dose and water_g:
        filter_ratio = (water_g / dose).quantize(Decimal("0.01"))

    brew = CoffeeBrew(
        user_id=current_user_id(),
        coffee_id=coffee.id if coffee else None,
        equipment_profile_id=_to_int(arguments.get("equipment_profile_id")),
        method=method,
        brew_context=brew_context,
        cafe_name=arguments.get("cafe_name"),
        drink_type=arguments.get("drink_type"),
        brewed_at=brewed_at,
        dose_g=dose,
        grind_setting=arguments.get("grind_setting"),
        water_temp_c=_to_decimal(arguments.get("water_temp_c")),
        yield_g=yield_g,
        time_s=_to_int(arguments.get("time_s")),
        pressure_bar=_to_decimal(arguments.get("pressure_bar")),
        ratio=ratio,
        water_g=water_g,
        brew_time_s=_to_int(arguments.get("brew_time_s")),
        filter_ratio=filter_ratio,
        acidity=_to_int(arguments.get("acidity")),
        sweetness=_to_int(arguments.get("sweetness")),
        body=_to_int(arguments.get("body")),
        bitterness=_to_int(arguments.get("bitterness")),
        overall=_to_int(arguments.get("overall")),
        flavour_notes=arguments.get("flavour_notes"),
        milk_drink=bool(arguments.get("milk_drink", False)),
        milk_type=arguments.get("milk_type"),
        milk_temp_c=_to_decimal(arguments.get("milk_temp_c")),
        extraction_assessment=arguments.get("extraction_assessment"),
        notes=arguments.get("notes"),
    )
    session.add(brew)
    session.commit()
    return json.dumps({
        "status": "ok",
        "brew": _brew_to_dict(brew),
    })


def handle_delete_brew(session: Session, arguments: dict[str, Any]) -> str:
    """Delete a brew session by id.

    N5: was `session.get(CoffeeBrew, brew_id)` — no `user_id` filter at
    all, so any caller could look up (the response echoes the full deleted
    row) and delete ANY user's brew by guessing a small integer id. Every
    other CoffeeBrew query in this module goes through `scoped_query`
    (`app/tools/helpers.py`'s "one user-scoping impl" — see server/CLAUDE.md);
    this was the one that didn't. Found by extending
    `tests/test_user_scoping.py`'s canary sweep to write tools.
    """
    brew_id = _to_int(arguments.get("brew_id"))
    if not brew_id:
        return json.dumps({"error": "brew_id is required"})
    brew = scoped_query(session, CoffeeBrew).filter(CoffeeBrew.id == brew_id).first()
    if not brew:
        return json.dumps({"error": f"brew {brew_id} not found"})
    deleted = _brew_to_dict(brew)
    session.delete(brew)
    session.commit()
    return json.dumps({"status": "deleted", "brew": deleted})


def handle_rate(session: Session, arguments: dict[str, Any]) -> str:
    """Update a coffee's rating and/or notes."""
    coffee = _resolve_coffee(session, arguments)
    if not coffee:
        return json.dumps({"error": "Coffee not found. Pass coffee_id, or coffee_name (+ roaster)."})

    if "rating" in arguments:
        coffee.rating = _to_int(arguments["rating"])
    if "notes" in arguments and arguments["notes"]:
        if coffee.notes:
            coffee.notes = f"{coffee.notes}\n\n{arguments['notes']}"
        else:
            coffee.notes = arguments["notes"]
    if "status" in arguments and arguments["status"]:
        coffee.status = arguments["status"]
    coffee.updated_at = datetime.now(timezone.utc)
    _enqueue_coffee_embedding(session, coffee)
    session.commit()
    return json.dumps({"status": "ok", "coffee": _coffee_to_dict(coffee)})


def handle_recommend(session: Session, arguments: dict[str, Any]) -> str:
    """Suggest what to try next based on history."""
    limit = min(int(arguments.get("limit", 5)), 20)

    # Top-rated favourites — anchor for "more like this"
    favourites = (
        session.query(Coffee)
        .filter(Coffee.rating.isnot(None), Coffee.rating >= 8)
        .order_by(Coffee.rating.desc())
        .limit(10)
        .all()
    )

    # Origins explored vs underexplored
    origin_counts = dict(
        session.query(Coffee.origin_country, sa_func.count(Coffee.id))
        .filter(Coffee.origin_country.isnot(None))
        .group_by(Coffee.origin_country)
        .all()
    )

    # Process/roaster favourites
    fav_processes = list({c.process for c in favourites if c.process})
    fav_roasters = list({c.roaster for c in favourites if c.roaster})

    # Untried-this-roaster suggestions: roasters you've loved but haven't bought from in a while
    last_purchase_by_roaster = dict(
        session.query(Coffee.roaster, sa_func.max(Coffee.created_at))
        .filter(Coffee.roaster.isnot(None))
        .group_by(Coffee.roaster)
        .all()
    )
    now = datetime.now(timezone.utc)
    stale_favourite_roasters = [
        r for r in fav_roasters
        if r in last_purchase_by_roaster
        and (now - last_purchase_by_roaster[r].astimezone(timezone.utc)).days > 60
    ]

    return json.dumps({
        "favourites": [
            {"id": c.id, "name": c.name, "roaster": c.roaster, "rating": c.rating}
            for c in favourites[:limit]
        ],
        "favourite_processes": fav_processes,
        "favourite_roasters": fav_roasters,
        "stale_favourite_roasters": stale_favourite_roasters,
        "origins_explored": [
            {"origin": o, "count": n} for o, n in
            sorted(origin_counts.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "suggestion_prompt": (
            "Look at favourites + favourite_processes + favourite_roasters. Suggest a "
            "coffee to try next that overlaps process/roaster but explores an underrepresented "
            "origin. Highlight stale_favourite_roasters as worth revisiting."
        ),
    })


def handle_current(session: Session, arguments: dict[str, Any]) -> str:
    """Coffees actively being drunk (status='current')."""
    rows = (
        session.query(Coffee)
        .filter(Coffee.status == "current")
        .order_by(Coffee.updated_at.desc())
        .all()
    )
    return json.dumps({
        "count": len(rows),
        "coffees": [_coffee_to_dict(c) for c in rows],
    })


def handle_similar(session: Session, arguments: dict[str, Any]) -> str:
    """Semantic search across the coffee library.

    Two modes:
      - query="..."          → free-text search (e.g. "stone fruit washed Ethiopian")
      - coffee_id / coffee_name → "more like this": uses the anchor coffee's
        embed text as the query, excludes the anchor from results.
    """
    from app.services.embedding import EmbeddingService

    query = (arguments.get("query") or "").strip()
    anchor: Coffee | None = None
    if not query:
        anchor = _resolve_coffee(session, arguments)
        if not anchor:
            return json.dumps({
                "error": "Pass either query=\"...\" OR coffee_id / coffee_name to anchor.",
            })
        query = _coffee_embed_text(anchor)
        if not query.strip():
            return json.dumps({"error": "Anchor coffee has no embeddable content."})

    limit = min(int(arguments.get("limit", 10)), 50)
    raw_limit = limit + 5  # overfetch so we can drop anchor + missing rows

    # R4 decay decision: OFF. This matches on flavor profile (free-text query,
    # or an anchor coffee's tasting notes) — recency is not a signal of a
    # better flavor match, and a bag logged a year ago that matches perfectly
    # must not lose to a middling match logged today.
    hits = EmbeddingService.search(
        session, query, sources=[EMBEDDING_SOURCE], limit=raw_limit,
        apply_recency_decay=False,
    )
    if not hits:
        return json.dumps({
            "query": query[:200],
            "count": 0,
            "results": [],
            "hint": "No coffee embeddings yet. Run coffee_embed to backfill.",
        })

    coffee_ids: list[int] = []
    score_by_id: dict[int, float] = {}
    for h in hits:
        try:
            cid = int(h["source_id"])
        except (ValueError, KeyError):
            continue
        if anchor and cid == anchor.id:
            continue
        coffee_ids.append(cid)
        score_by_id[cid] = h["score"]

    if not coffee_ids:
        return json.dumps({"query": query[:200], "count": 0, "results": []})

    coffees = {c.id: c for c in session.query(Coffee).filter(Coffee.id.in_(coffee_ids)).all()}
    enriched = []
    for cid in coffee_ids:
        c = coffees.get(cid)
        if not c:
            continue
        row = _coffee_to_dict(c)
        row["score"] = score_by_id.get(cid)
        enriched.append(row)
        if len(enriched) >= limit:
            break

    return json.dumps({
        "query": query[:200],
        "anchor_id": anchor.id if anchor else None,
        "count": len(enriched),
        "results": enriched,
    })


def handle_embed(session: Session, arguments: dict[str, Any]) -> str:
    """Backfill embeddings for all coffees that lack one (or have stale content)."""
    only_missing = bool(arguments.get("only_missing", True))
    limit = int(arguments.get("limit", 1000))

    coffees_q = session.query(Coffee).order_by(Coffee.id.asc())
    if only_missing:
        from app.services.embedding import Embedding
        embedded_ids = {
            row[0] for row in session.query(Embedding.source_id)
            .filter(Embedding.source == EMBEDDING_SOURCE).all()
        }
    else:
        embedded_ids = set()

    enqueued = 0
    skipped = 0
    seen = 0
    for c in coffees_q.limit(limit):
        seen += 1
        if str(c.id) in embedded_ids:
            skipped += 1
            continue
        if _enqueue_coffee_embedding(session, c):
            enqueued += 1
        else:
            skipped += 1
    session.commit()
    return json.dumps({
        "status": "ok",
        "seen": seen,
        "enqueued": enqueued,
        "skipped": skipped,
        "note": "Embedding worker will process the queue within ~5 min.",
    })


def handle_dial_in(session: Session, arguments: dict[str, Any]) -> str:
    """AI dial-in suggestion for the next brew (TODO: wire up Anthropic call).

    Currently returns the structured context that would be sent to Claude.
    The MCP client (Claude itself) can then synthesise the suggestion.
    """
    coffee = _resolve_coffee(session, arguments)
    if not coffee:
        return json.dumps({"error": "Coffee not found."})

    brews = (
        scoped_query(session, CoffeeBrew)
        .filter(CoffeeBrew.coffee_id == coffee.id)
        .order_by(CoffeeBrew.brewed_at.desc())
        .limit(5)
        .all()
    )
    equipment = None
    if brews and brews[0].equipment_profile_id:
        equipment = session.get(CoffeeEquipmentProfile, brews[0].equipment_profile_id)
    elif (eid := arguments.get("equipment_profile_id")) is not None:
        equipment = session.get(CoffeeEquipmentProfile, int(eid))

    return json.dumps({
        "coffee": _coffee_to_dict(coffee),
        "recent_brews": [_brew_to_dict(b) for b in brews],
        "equipment": (
            {
                "name": equipment.name,
                "method": equipment.method,
                "grinder": equipment.grinder_name,
                "grinder_type": equipment.grinder_type,
                "brewer": equipment.brewer_name,
                "has_pid": equipment.has_pid,
                "defaults": {
                    "dose_g": float(equipment.default_dose_g) if equipment.default_dose_g else None,
                    "yield_g": float(equipment.default_yield_g) if equipment.default_yield_g else None,
                    "water_g": float(equipment.default_water_g) if equipment.default_water_g else None,
                    "temp_c": float(equipment.default_temp_c) if equipment.default_temp_c else None,
                    "time_s": equipment.default_time_s,
                },
            }
            if equipment else None
        ),
        "guidance": (
            "Specialty coffee dial-in. Adjust ONE variable at a time. "
            "Sour/sharp = under-extracted (finer grind, higher temp, longer time). "
            "Bitter/astringent = over-extracted (coarser grind, lower temp, shorter time). "
            "Hollow = need more dose or higher ratio. Suggest target_grind, target_yield_g, "
            "target_temp_c, target_time_s, and a one-line reasoning per change."
        ),
    })


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def get_mcp_tools() -> list[dict]:
    return [
        SearchTool(
            name="coffee_search",
            description=(
                "Search the coffee library by name, roaster, origin, region, process, "
                "variety, or tasting notes. Case-insensitive partial matching across all "
                "text fields. Returns full coffee detail."
            ),
            model=Coffee,
            search_columns=[
                "name", "roaster", "origin_country", "region_farm",
                "process", "variety", "roaster_tasting_notes", "notes",
            ],
            timestamp_col="created_at",
            to_dict=_coffee_to_dict,
            category="coffee",
            examples=[
                "Find Ethiopian naturals I've tried",
                "Search Cloud Picker coffees",
                "Coffees with stone fruit notes",
            ],
        ).build(),

        ListTool(
            name="coffee_recent_brews",
            description=(
                "Recent brew sessions, newest first. Returns method, recipe, and tasting "
                "scores. Filter by date range to see brews in a specific period."
            ),
            model=CoffeeBrew,
            timestamp_col="brewed_at",
            to_dict=_brew_to_dict,
            date_params=("from_date", "to_date"),
            category="coffee",
            examples=[
                "What did I brew this week?",
                "Show me brews from last month",
            ],
        ).build(),

        StatsTool(
            name="coffee_stats",
            description=(
                "Coffee library + brew session statistics: counts by origin, process, "
                "roaster (with avg rating), and method (with avg score). Period filter "
                "applies to created_at on coffees and brewed_at on brews."
            ),
            model=Coffee,
            compute=_compute_stats,
            input_schema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["this_week", "this_month", "this_year", "all_time", "30day", "90day", "1year"],
                        "default": "all_time",
                    },
                },
            },
            category="coffee",
            examples=[
                "What origins have I drunk most?",
                "Coffee stats for this year",
            ],
        ).build(),

        CustomTool(
            name="coffee_log",
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            description=(
                "Add or update a coffee bag. Upserts by (name, roaster). Pass any subset "
                "of fields — only provided fields are updated. Use status='current' for "
                "what you're drinking, 'finished' when done, 'incoming' for ordered, "
                "'freezer' for stored, 'wishlist' for want-to-try."
            ),
            input_schema={
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "roaster": {"type": "string"},
                    "origin_country": {"type": "string"},
                    "region_farm": {"type": "string"},
                    "process": {"type": "string", "description": "Washed, Natural, Honey, Anaerobic Natural, Blend, etc."},
                    "fermentation": {"type": "string"},
                    "variety": {"type": "string"},
                    "altitude_masl": {"type": "string"},
                    "category": {"type": "string", "description": "Roast level: Light, Medium, Dark"},
                    "weight_g": {"type": ["number", "string"]},
                    "price": {"type": ["number", "string"]},
                    "roaster_tasting_notes": {"type": "string"},
                    "notes": {"type": "string", "description": "Personal tasting notes"},
                    "rating": {"type": ["integer", "string"], "description": "0-10"},
                    "status": {
                        "type": "string",
                        "enum": ["current", "finished", "freezer", "incoming", "wishlist"],
                    },
                    "roast_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "purchase_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "photo_url": {"type": "string"},
                },
            },
            handler=handle_log,
            category="coffee",
        ).build(),

        CustomTool(
            name="coffee_brew",
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            description=(
                "Log a brew session. Resolve coffee by coffee_id OR coffee_name (+ optional "
                "roaster). For cafe brews set brew_context='cafe', cafe_name, drink_type — "
                "coffee_id can be null. ratio (espresso) and filter_ratio (filter) are "
                "calculated from dose+yield/water automatically."
            ),
            input_schema={
                "type": "object",
                "required": ["method"],
                "properties": {
                    "method": {"type": "string", "enum": ["espresso", "filter"]},
                    "brew_context": {"type": "string", "enum": ["home", "cafe"], "default": "home"},
                    "coffee_id": {"type": ["integer", "string"]},
                    "coffee_name": {"type": "string"},
                    "roaster": {"type": "string"},
                    "equipment_profile_id": {"type": ["integer", "string"]},
                    "cafe_name": {"type": "string"},
                    "drink_type": {"type": "string"},
                    "brewed_at": {"type": "string", "description": "ISO 8601; defaults to now"},
                    "dose_g": {"type": ["number", "string"]},
                    "grind_setting": {"type": "string"},
                    "water_temp_c": {"type": ["number", "string"]},
                    "yield_g": {"type": ["number", "string"], "description": "Espresso"},
                    "time_s": {"type": ["integer", "string"], "description": "Espresso"},
                    "pressure_bar": {"type": ["number", "string"]},
                    "water_g": {"type": ["number", "string"], "description": "Filter"},
                    "brew_time_s": {"type": ["integer", "string"], "description": "Filter"},
                    "acidity": {"type": ["integer", "string"], "description": "1-5"},
                    "sweetness": {"type": ["integer", "string"], "description": "1-5"},
                    "body": {"type": ["integer", "string"], "description": "1-5"},
                    "bitterness": {"type": ["integer", "string"], "description": "1-5"},
                    "overall": {"type": ["integer", "string"], "description": "1-5"},
                    "flavour_notes": {"type": "array", "items": {"type": "string"}},
                    "milk_drink": {"type": "boolean"},
                    "milk_type": {"type": "string"},
                    "milk_temp_c": {"type": ["number", "string"]},
                    "extraction_assessment": {"type": "string", "enum": ["under", "good", "over"]},
                    "notes": {"type": "string"},
                },
            },
            handler=handle_brew,
            category="coffee",
        ).build(),

        CustomTool(
            name="coffee_delete_brew",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
            description=(
                "Delete a brew session by id. Use to remove duplicates or mistakes. "
                "Returns the deleted row so the action is auditable."
            ),
            input_schema={
                "type": "object",
                "required": ["brew_id"],
                "properties": {
                    "brew_id": {"type": ["integer", "string"]},
                },
            },
            handler=handle_delete_brew,
            category="coffee",
        ).build(),

        CustomTool(
            name="coffee_rate",
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            description=(
                "Update a coffee's overall rating (0-10), append notes, or change status. "
                "Resolve by coffee_id OR coffee_name (+ optional roaster)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "coffee_id": {"type": ["integer", "string"]},
                    "coffee_name": {"type": "string"},
                    "roaster": {"type": "string"},
                    "rating": {"type": ["integer", "string"]},
                    "notes": {"type": "string", "description": "Appended to existing notes"},
                    "status": {
                        "type": "string",
                        "enum": ["current", "finished", "freezer", "incoming", "wishlist"],
                    },
                },
            },
            handler=handle_rate,
            category="coffee",
        ).build(),

        CustomTool(
            name="coffee_recommend",
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            description=(
                "Suggest what to try next. Returns favourites (rating ≥ 8), favourite "
                "processes/roasters, origin coverage, and stale favourite roasters (loved "
                "but not purchased in 60+ days). Includes a synthesis prompt for the model."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": ["integer", "string"], "default": 5},
                },
            },
            handler=handle_recommend,
            category="coffee",
        ).build(),

        CustomTool(
            name="coffee_current",
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            description=(
                "List coffees marked status='current' — the bag(s) you're actively "
                "drinking. Used by the daily-note morning briefing."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=handle_current,
            category="coffee",
        ).build(),

        CustomTool(
            name="coffee_similar",
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            description=(
                "Semantic search across the coffee library. Two modes: pass query=\"...\" "
                "for free-text search (e.g. \"juicy washed Ethiopian with stone fruit\"), "
                "or pass coffee_id / coffee_name to find coffees similar to that anchor. "
                "Uses embeddings of name + roaster + origin + region + process + variety + "
                "tasting notes. Run coffee_embed first if results are empty."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "coffee_id": {"type": ["integer", "string"]},
                    "coffee_name": {"type": "string"},
                    "roaster": {"type": "string"},
                    "limit": {"type": ["integer", "string"], "default": 10},
                },
            },
            handler=handle_similar,
            category="coffee",
            examples=[
                "Find coffees similar to my favourite Honey Granada",
                "Search semantically for fruity natural processes",
            ],
        ).build(),

        CustomTool(
            name="coffee_embed",
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            description=(
                "Backfill embeddings for coffees lacking one. Admin tool — run once after "
                "import_brewhaha or after bulk edits. By default only embeds coffees that "
                "don't already have an embedding (only_missing=true)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "only_missing": {"type": "boolean", "default": True},
                    "limit": {"type": ["integer", "string"], "default": 1000},
                },
            },
            handler=handle_embed,
            category="coffee",
        ).build(),

        CustomTool(
            name="coffee_dial_in",
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            description=(
                "Returns structured context (coffee profile + recent brews + equipment) "
                "for the model to synthesise a one-variable-at-a-time dial-in suggestion. "
                "Includes the specialty-coffee guidance heuristic. Resolve by coffee_id or "
                "coffee_name (+ roaster)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "coffee_id": {"type": ["integer", "string"]},
                    "coffee_name": {"type": "string"},
                    "roaster": {"type": "string"},
                    "equipment_profile_id": {"type": ["integer", "string"]},
                },
            },
            handler=handle_dial_in,
            category="coffee",
        ).build(),
    ]
