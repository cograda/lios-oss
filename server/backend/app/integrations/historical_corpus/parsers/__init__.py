"""Format-specific parsers for historical corpus ingestion.

Each parser exposes parse(path) -> (doc_meta, list[ChunkRecord]).
Shared types live in types.py.
"""

from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta

__all__ = ["ChunkRecord", "DocMeta"]
