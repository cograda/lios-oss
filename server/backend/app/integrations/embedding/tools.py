"""Cross-source semantic search + stats tools.

Moved out of `app/mcp/server.py::_register_embedding_tools()` (V4 chunk 3.4)
into the normal `mcp_tools()` integration pattern — `search_semantic` and
`search_stats` are byte-identical in name, description, and schema to their
pre-chunk versions; only which package registers them changed.

V4 chunk 4.3e: tool dicts built via the declarative DSL's `CustomTool`
wrapper (missed by chunk 3.4) rather than hand-assembled — names,
descriptions, schemas, and annotations are unchanged, only the construction
mechanism.
"""

import json
from typing import Any

from app.tools import CustomTool, ToolAnnotations
from app.tools.base import parse_iso_date

# Lazy-imported inside each handler, not at module level: this module is
# imported by app.integrations.embedding's package __init__ (via
# get_mcp_tools), and app.services.embedding imports
# app.integrations.embedding.models — a top-level import here would be
# circular (services/embedding.py mid-import -> imports this package ->
# imports this module -> imports services/embedding.py again, before
# EmbeddingService exists).


def _build_cross_source_date_filter(after_dt, before_dt):
    """Build the after/before filter clause for `search_semantic`.

    Unlike gmail_semantic_search/whatsapp_semantic_search (single source, so
    the "date" column is unambiguous), this tool spans sources with genuinely
    different date semantics — mail has a sent date, whatsapp a message
    timestamp, vault/corpus/coffee have no per-item date embedded at all.
    There's no single column to filter on, and no schema change is in scope
    here to add one.

    The choice made: filter email and whatsapp rows against their real dates
    (same logic as their dedicated tools, via a lazy import to avoid a
    module-level dependency from this cross-source aggregator on two other
    integrations' internals), and let every other source's rows pass through
    unfiltered rather than silently vanish because they have no date to
    compare. This means a date range narrows email/whatsapp results but does
    NOT narrow vault/corpus/coffee results — documented on the tool
    description below rather than left to be discovered as a surprise.
    """
    from sqlalchemy import and_, or_

    from app.integrations.google_mail.facade import FACADE as _mail_facade
    from app.integrations.whatsapp.facade import FACADE as _wa_facade
    from app.services.embedding import Embedding

    mail_clause = and_(Embedding.source == "email", _mail_facade.date_filter_clause(after_dt, before_dt))
    wa_clause = and_(Embedding.source == "whatsapp", _wa_facade.date_filter_clause(after_dt, before_dt))
    other_sources = ~Embedding.source.in_(["email", "whatsapp"])
    return or_(other_sources, mail_clause, wa_clause)


def _handle_semantic_search(session, arguments: dict) -> str:
    from app.services.embedding import EmbeddingService

    query = (arguments.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    sources = arguments.get("sources")  # None = all, or list like ["vault", "email"]
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",")]
    limit = min(int(arguments.get("limit", 10)), 50)

    after_val = parse_iso_date(arguments.get("after"))
    before_val = parse_iso_date(arguments.get("before"))
    date_range_requested = after_val is not None or before_val is not None
    extra_filter = _build_cross_source_date_filter(after_val, before_val) if date_range_requested else None

    results = EmbeddingService.search(
        session, query=query, sources=sources, limit=limit, extra_filter=extra_filter,
    )

    if not results and date_range_requested:
        # Not an `error`: filtering to nothing is a legitimate outcome, and the
        # success path returns a bare list, so a caller branching on "error" in
        # the payload would read this as a failure. Carries a `note` instead so
        # the caller can say *why* it found nothing rather than guessing whether
        # the filter or the query was responsible.
        return json.dumps({
            "results": [],
            "note": (
                f"No results in that date range (after={arguments.get('after')!r}, "
                f"before={arguments.get('before')!r}). The date range only "
                "narrows email and WhatsApp results — other sources have no "
                "reliable per-item date and are unaffected by it. Try widening "
                "or omitting the date range."
            ),
        })

    return json.dumps(results, indent=2)


def _handle_embedding_stats(session, arguments: dict) -> str:
    from app.services.embedding import EmbeddingService

    stats = EmbeddingService.stats(session)
    return json.dumps(stats, indent=2)


def get_mcp_tools() -> list[dict[str, Any]]:
    return [
        CustomTool(
            name="search_semantic",
            description=(
                "Cross-source semantic search across all embedded content — vault notes, "
                "emails, and WhatsApp messages. Finds content by meaning, not just keywords. "
                "Use this for broad searches when you don't know which source has the answer. "
                "For source-specific search, use vault_search, gmail_semantic_search, or "
                "whatsapp_semantic_search instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query.",
                    },
                    "sources": {
                        "type": "string",
                        "description": "Comma-separated sources to search (e.g. 'vault,email,whatsapp'). Omit for all sources.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, max 50).",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            handler=_handle_semantic_search,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="search_stats",
            description=(
                "Unified embedding pipeline statistics: total embeddings per source "
                "(vault, email, WhatsApp), queue status, and model info. "
                "Admin tool for checking search index health."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_handle_embedding_stats,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
    ]
