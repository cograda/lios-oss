"""Shared dataclasses produced by every parser.

DocMeta describes the source file; ChunkRecord is one embeddable slice.
The ingestion orchestrator fans these into historical_documents +
historical_document_chunks rows and enqueues chunks for embedding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class DocMeta:
    title: str
    source_type: str
    author: str | None = None
    participants: list[str] = field(default_factory=list)
    document_date: date | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ChunkRecord:
    chunk_type: str
    chunk_text: str
    breadcrumb: str | None = None
    metadata: dict = field(default_factory=dict)


# Every `DocMeta.source_type` a producer writes, in one place. The parsers keep
# their own literals (that is what a parser *is*: the thing that knows its
# type), so this tuple is a declaration that can drift — which is why
# tests/test_corpus_source_types.py derives the real set from the producers'
# source and asserts it equals this one. Add a parser, forget to list it here,
# and the suite fails rather than the MCP schema quietly omitting the type.
#
# `corpus_search`'s `source_types` schema enum renders from this tuple. Until
# 2026-09-06 that list was hand-typed prose and had drifted: `manual`,
# `voice_memo` and `claude_conversation` were all missing.
# `attachments/ingest.py` writes `{prefix}_{kind}` — one prefix per message
# source (`ingest.SOURCE_TYPE_PREFIX`), one kind per parser in its MIME table.
ATTACHMENT_SOURCE_PREFIXES: tuple[str, ...] = ("wa_attachment", "gmail_attachment")
WHATSAPP_ATTACHMENT_KINDS: tuple[str, ...] = ("pdf", "docx", "xlsx")

KNOWN_SOURCE_TYPES: tuple[str, ...] = (
    "whatsapp_txt",
    "email_json",
    "pdf",
    "docx",
    "boq_xlsx",
    "manual",
    "voice_memo",
    "claude_conversation",
    *(
        f"{prefix}_{kind}"
        for prefix in ATTACHMENT_SOURCE_PREFIXES
        for kind in WHATSAPP_ATTACHMENT_KINDS
    ),
)
