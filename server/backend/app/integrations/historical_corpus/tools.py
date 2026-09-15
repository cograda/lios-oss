"""MCP tools for querying the historical corpus.

Primary tool: corpus_search — semantic search over the ingested document
corpus with enrichment from HistoricalDocument (title, date, source_type,
author, participants, breadcrumb).

Renamed from `riverside_context` on 2026-07-28: the old name put a family
renovation project into the public tool surface. The project a document
belongs to is now data (`HistoricalDocument.project_tags`, defaulted from
the `default_project_tag` config key), not part of the tool's identity.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from app.auth.context import current_user_id_or_none
from app.integrations.historical_corpus.ingest import EMBEDDING_SOURCE
from app.integrations.historical_corpus.models import (
    HistoricalDocument, HistoricalDocumentChunk,
)
from app.integrations.historical_corpus.parsers.types import KNOWN_SOURCE_TYPES
from app.services.embedding import Embedding, EmbeddingService
from app.tools import CustomTool, ToolAnnotations


def source_type_filter(source_types: list[str]):
    """A WHERE clause restricting `Embedding` rows to chunks of these source types.

    Passed to `EmbeddingService.search(extra_filter=...)`, so the restriction is
    applied *inside* the vector query — before its ORDER BY distance / LIMIT —
    and the top-K is the best K chunks *of the wanted types*.

    Until 2026-09-06 `corpus_search` did this as a post-filter: fetch
    `min(limit*3, 50)` hits, then drop rows whose document was the wrong type.
    Measured on the live corpus that day: "dishwasher filter cleaning and salt
    refill" with `source_types=["manual"]` returned **0 results**, while the
    same query unfiltered ranked the Neff dishwasher manual 5th behind WhatsApp
    chatter. Manuals are 2,065 of 24,429 chunks (8.5%); a type that ranks below
    the noise in the top 50 could never be filtered *into* view, only out of it.
    A filter that can only narrow what an unfiltered search already found is
    not a filter, it is a display option.

    Mechanism: a semi-join back to the corpus's own tables rather than the
    per-chunk `metadata_json` copy of `source_type`. `historical_documents.
    source_type` is the row the post-filter compared against and the one a
    re-ingest updates, so this is the same predicate the old code applied —
    just applied where it can change the answer. `Embedding.source_id` is
    `"{document_id}:{chunk_index}"` (see ingest._upsert_document), rebuilt
    here on the chunk side so no cast is ever attempted on a non-corpus
    `source_id` — Postgres may evaluate WHERE terms in any order, and
    `split_part(source_id, ':', 1)::int` would error on `Note.md#3`.

    Index implications: the semi-join is a hashed subplan over the wanted
    types (`historical_documents.source_type` and
    `historical_document_chunks.document_id` are both indexed, and the
    largest type is ~14k rows), evaluated once per candidate the HNSW walk
    yields. `_enable_iterative_scan` then keeps the index walk going until
    `limit` candidates *pass* the filter — the same mechanism every other
    filtered search here relies on. For a very rare type the walk can hit
    pgvector's `hnsw.max_scan_tuples` (default 20,000) before finding
    `limit` matches and return fewer; that is a fewer-rows-than-asked
    outcome, never a wrong-rows one, and the planner may choose an exact
    scan instead when the filter is selective enough.
    """
    wanted_ids = (
        select(
            cast(HistoricalDocumentChunk.document_id, String)
            + ":"
            + cast(HistoricalDocumentChunk.chunk_index, String)
        )
        .join(
            HistoricalDocument,
            HistoricalDocument.id == HistoricalDocumentChunk.document_id,
        )
        .where(HistoricalDocument.source_type.in_(list(source_types)))
    )
    return Embedding.source_id.in_(wanted_ids)


def visible_to_caller():
    """WHERE clause: documents the bound caller may read.

    A document with `owner_user_id` NULL is household-shared; one with an
    owner is that user's alone. An unbound caller (a background job) sees
    shared documents only — the same rule `EmbeddingService.search` applies
    to embedding rows, restated on the document so a per-document read path
    can refuse independently of how the hit was found.
    """
    uid = current_user_id_or_none()
    if uid is None:
        return HistoricalDocument.owner_user_id.is_(None)
    return or_(
        HistoricalDocument.owner_user_id.is_(None),
        HistoricalDocument.owner_user_id == uid,
    )


def _enrich_hits(
    session: Session,
    hits: list[dict],
    *,
    dedupe: bool = True,
) -> list[dict]:
    """Given raw embedding hits, fetch doc/chunk rows and stitch metadata.

    When `dedupe` is on, collapse hits that share a chunk content_hash (common
    for BoQ — "Copy of …", "Sam edits", "DRAFT" versions of the same document
    emit byte-identical line-item chunks). The highest-scoring hit is returned
    and the duplicates are surfaced as `alternate_sources` so the caller can
    still see which docs carry the same content.

    Ownership (2026-09-06): the document fetch is restricted to what the
    caller may read (`visible_to_caller`). The vector search already excludes
    another user's chunks via `embeddings.user_id`, so in the normal case
    this drops nothing — it is the second lock, for a hit that arrived by
    any route other than that search (a stale NULL embedding row, a caller
    passing hits it assembled itself). An owned document a non-owner is not
    allowed to see is simply absent from the result, never partially
    rendered.
    """
    if not hits:
        return []

    # Parse "doc_id:chunk_idx" and batch-fetch the chunks (and their docs).
    pairs: list[tuple[int, int]] = []
    for h in hits:
        try:
            doc_id_s, chunk_idx_s = h["source_id"].split(":", 1)
            pairs.append((int(doc_id_s), int(chunk_idx_s)))
        except (ValueError, KeyError):
            continue

    if not pairs:
        return []

    doc_ids = sorted({d for d, _ in pairs})
    docs_by_id: dict[int, HistoricalDocument] = {
        d.id: d for d in session.query(HistoricalDocument)
        .filter(HistoricalDocument.id.in_(doc_ids))
        .filter(visible_to_caller())
        .all()
    }
    chunks_by_key: dict[tuple[int, int], HistoricalDocumentChunk] = {
        (c.document_id, c.chunk_index): c
        for c in session.query(HistoricalDocumentChunk).filter(
            HistoricalDocumentChunk.document_id.in_(doc_ids)
        ).all()
    }

    enriched: list[dict] = []
    seen_by_hash: dict[str, dict] = {}
    for h, (doc_id, chunk_idx) in zip(hits, pairs):
        doc = docs_by_id.get(doc_id)
        chunk = chunks_by_key.get((doc_id, chunk_idx))
        if not doc or not chunk:
            continue

        alt = {
            "source_type": doc.source_type,
            "title": doc.title,
            "source_path": doc.source_path,
            "document_date": doc.document_date.isoformat() if doc.document_date else None,
        }

        if dedupe and chunk.content_hash in seen_by_hash:
            seen_by_hash[chunk.content_hash]["alternate_sources"].append(alt)
            continue

        row = {
            "score": h["score"],
            "source_type": doc.source_type,
            "title": doc.title,
            "document_date": doc.document_date.isoformat() if doc.document_date else None,
            "author": doc.author,
            "participants": doc.participants,
            "source_path": doc.source_path,
            "chunk_type": chunk.chunk_type,
            "breadcrumb": chunk.breadcrumb,
            "preview": (chunk.chunk_text or "")[:500],
            "chunk_metadata": chunk.chunk_metadata,
            "alternate_sources": [],
        }
        enriched.append(row)
        if dedupe:
            seen_by_hash[chunk.content_hash] = row
    return enriched


def corpus_search_handler(session: Session, arguments: dict[str, Any]) -> str:
    query = (arguments.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    limit = int(arguments.get("limit") or 20)
    source_types = arguments.get("source_types") or None
    if isinstance(source_types, str):
        source_types = [source_types]
    if source_types:
        unknown = sorted(set(source_types) - set(KNOWN_SOURCE_TYPES))
        if unknown:
            # Loud, not empty: an unknown type would otherwise return zero rows
            # and read exactly like "nothing in the corpus matches".
            return json.dumps({
                "error": f"unknown source_types: {unknown}",
                "known_source_types": list(KNOWN_SOURCE_TYPES),
            })

    # The source-type restriction rides INTO the vector query (see
    # `source_type_filter`), so no over-fetch is needed and the unfiltered
    # path is the exact single ORDER BY/LIMIT it always was.
    # R4 decay decision: OFF. This corpus is a household *archive* by design
    # (renovation paperwork, comms, meeting notes) — an old document isn't
    # lower-quality evidence than a new one, and penalising it by age would
    # bury exactly the records this tool exists to surface. Recency decay
    # is for "what's true now"; this is "what was said/decided".
    hits = EmbeddingService.search(
        session, query, sources=[EMBEDDING_SOURCE], limit=limit,
        extra_filter=source_type_filter(source_types) if source_types else None,
        apply_recency_decay=False,
    )
    enriched = _enrich_hits(session, hits)[:limit]
    return json.dumps({"query": query, "count": len(enriched), "results": enriched}, default=str)


def claude_history_search_handler(session: Session, arguments: dict[str, Any]) -> str:
    query = (arguments.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    limit = int(arguments.get("limit") or 20)
    # R4 decay decision: OFF, same reasoning as corpus_search_handler above —
    # this searches historical claude.ai conversation exports specifically,
    # where "old" is the whole point, not a defect.
    # Same retrieval-side restriction as corpus_search: the top-K is the best K
    # conversation chunks, not whatever survives of a mixed top-50.
    hits = EmbeddingService.search(
        session, query, sources=[EMBEDDING_SOURCE], limit=limit,
        extra_filter=source_type_filter(["claude_conversation"]),
        apply_recency_decay=False,
    )
    enriched = _enrich_hits(session, hits)[:limit]
    return json.dumps({"query": query, "count": len(enriched), "results": enriched}, default=str)


def corpus_stats_handler(session: Session, arguments: dict[str, Any]) -> str:
    """Summary of what's been ingested — what THIS caller can search.

    Counts are restricted to documents visible to the caller (see
    `visible_to_caller`), so the figure a person is shown is the corpus their
    searches actually run over; a source type they cannot read does not
    appear as a row at all. The admin dashboard (`dashboard_data`) keeps the
    whole-table count.
    """
    from sqlalchemy import func as sa_func

    visible = visible_to_caller()
    doc_rows = (
        session.query(
            HistoricalDocument.source_type,
            sa_func.count(HistoricalDocument.id).label("docs"),
            sa_func.min(HistoricalDocument.document_date).label("earliest"),
            sa_func.max(HistoricalDocument.document_date).label("latest"),
        )
        .filter(visible)
        .group_by(HistoricalDocument.source_type)
        .all()
    )
    chunk_rows = (
        session.query(
            HistoricalDocumentChunk.chunk_type,
            sa_func.count(HistoricalDocumentChunk.id).label("chunks"),
        )
        .join(HistoricalDocument, HistoricalDocument.id == HistoricalDocumentChunk.document_id)
        .filter(visible)
        .group_by(HistoricalDocumentChunk.chunk_type)
        .all()
    )
    total_docs = (
        session.query(sa_func.count(HistoricalDocument.id)).filter(visible).scalar() or 0
    )
    total_chunks = (
        session.query(sa_func.count(HistoricalDocumentChunk.id))
        .join(HistoricalDocument, HistoricalDocument.id == HistoricalDocumentChunk.document_id)
        .filter(visible)
        .scalar() or 0
    )

    return json.dumps({
        "total_documents": total_docs,
        "total_chunks": total_chunks,
        "by_source_type": [
            {
                "source_type": r.source_type, "documents": r.docs,
                "earliest": r.earliest.isoformat() if r.earliest else None,
                "latest": r.latest.isoformat() if r.latest else None,
            } for r in doc_rows
        ],
        "by_chunk_type": [
            {"chunk_type": r.chunk_type, "chunks": r.chunks} for r in chunk_rows
        ],
    }, default=str)


_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)


def mcp_tools() -> list[dict[str, Any]]:
    return [
        CustomTool(
            name="corpus_search",
            description=(
                "Search the ingested historical document corpus (WhatsApp exports, "
                "email, bills of quantities, PDFs, docx) with semantic retrieval. "
                "Returns chunks with document title, date, breadcrumb, and "
                "source_path. Use this when the user asks about past decisions, "
                "prior correspondence, quoted rates, or meeting context that "
                "predates the live integrations."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50},
                    "source_types": {
                        "type": "array",
                        # Rendered from the registry, never typed by hand — the
                        # hand-typed version omitted three of the eleven types.
                        "items": {"type": "string", "enum": list(KNOWN_SOURCE_TYPES)},
                        "description": (
                            "Optional: restrict retrieval to these source types "
                            "(applied inside the vector search, so the top-K is "
                            "the best K chunks of the wanted types). One or more "
                            "of: " + ", ".join(KNOWN_SOURCE_TYPES) + "."
                        ),
                    },
                },
                "required": ["query"],
            },
            handler=corpus_search_handler,
            annotations=_READ_ONLY,
        ).build(),
        CustomTool(
            name="claude_history_search",
            # "your", not a name: the history is private to whoever exported
            # it (2026-09-06 — `historical_documents.owner_user_id`), so a
            # caller with no export ingested gets an empty result, not
            # someone else's chats.
            description=(
                "Semantic search over your archived Claude.ai conversation history "
                "(chats exported from claude.ai and ingested as yours — private to "
                "you, never another household member's). Use this when the user asks "
                "what they discussed with Claude previously, wants to recall a past "
                "chat, or references something they remember asking an AI about "
                "before comar existed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
            handler=claude_history_search_handler,
            annotations=_READ_ONLY,
        ).build(),
        CustomTool(
            name="corpus_stats",
            description="Report what's been ingested into the historical corpus.",
            input_schema={"type": "object", "properties": {}},
            handler=corpus_stats_handler,
            annotations=_READ_ONLY,
        ).build(),
    ]
