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

from sqlalchemy.orm import Session

from app.integrations.historical_corpus.ingest import EMBEDDING_SOURCE
from app.integrations.historical_corpus.models import (
    HistoricalDocument, HistoricalDocumentChunk,
)
from app.services.embedding import EmbeddingService
from app.tools import CustomTool, ToolAnnotations


def _enrich_hits(
    session: Session,
    hits: list[dict],
    source_types: list[str] | None,
    *,
    dedupe: bool = True,
) -> list[dict]:
    """Given raw embedding hits, fetch doc/chunk rows and stitch metadata.

    When `dedupe` is on, collapse hits that share a chunk content_hash (common
    for BoQ — "Copy of …", "Sam edits", "DRAFT" versions of the same document
    emit byte-identical line-item chunks). The highest-scoring hit is returned
    and the duplicates are surfaced as `alternate_sources` so the caller can
    still see which docs carry the same content.
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
        .filter(HistoricalDocument.id.in_(doc_ids)).all()
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
        if source_types and doc.source_type not in source_types:
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

    # Over-fetch so filtering by source_type still returns enough results.
    raw_limit = min(limit * 3, 50) if source_types else limit
    hits = EmbeddingService.search(
        session, query, sources=[EMBEDDING_SOURCE], limit=raw_limit,
    )
    enriched = _enrich_hits(session, hits, source_types)[:limit]
    return json.dumps({"query": query, "count": len(enriched), "results": enriched}, default=str)


def claude_history_search_handler(session: Session, arguments: dict[str, Any]) -> str:
    query = (arguments.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    limit = int(arguments.get("limit") or 20)
    hits = EmbeddingService.search(
        session, query, sources=[EMBEDDING_SOURCE], limit=min(limit * 3, 50),
    )
    enriched = _enrich_hits(session, hits, ["claude_conversation"])[:limit]
    return json.dumps({"query": query, "count": len(enriched), "results": enriched}, default=str)


def corpus_stats_handler(session: Session, arguments: dict[str, Any]) -> str:
    """Summary of what's been ingested. Useful for debugging and the dashboard."""
    from sqlalchemy import func as sa_func

    doc_rows = (
        session.query(
            HistoricalDocument.source_type,
            sa_func.count(HistoricalDocument.id).label("docs"),
            sa_func.min(HistoricalDocument.document_date).label("earliest"),
            sa_func.max(HistoricalDocument.document_date).label("latest"),
        )
        .group_by(HistoricalDocument.source_type)
        .all()
    )
    chunk_rows = (
        session.query(
            HistoricalDocumentChunk.chunk_type,
            sa_func.count(HistoricalDocumentChunk.id).label("chunks"),
        )
        .group_by(HistoricalDocumentChunk.chunk_type)
        .all()
    )
    total_docs = session.query(sa_func.count(HistoricalDocument.id)).scalar() or 0
    total_chunks = session.query(sa_func.count(HistoricalDocumentChunk.id)).scalar() or 0

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
                        "items": {"type": "string"},
                        "description": (
                            "Optional: restrict to these source types. "
                            "One or more of: whatsapp_txt, email_json, pdf, docx, "
                            "boq_xlsx, wa_attachment_pdf, wa_attachment_docx, "
                            "wa_attachment_xlsx."
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
            description=(
                "Semantic search over Alex's archived Claude.ai conversation history "
                "(5,000+ chats going back to 2024, exported from claude.ai). Use this "
                "when the user asks what they discussed with Claude previously, wants "
                "to recall a past chat, or references something they remember asking "
                "an AI about before comar existed."
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
