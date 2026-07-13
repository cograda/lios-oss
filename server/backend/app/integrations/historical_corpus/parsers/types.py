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
