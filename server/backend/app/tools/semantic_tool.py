"""SemanticSearchTool — declarative builder for embedding-backed search tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.tools.base import ToolAnnotations, ToolBuilder


class SemanticSearchTool(ToolBuilder):
    """Generates a semantic search tool backed by EmbeddingService.

    Replaces hand-written handlers like gmail_semantic_search, whatsapp_semantic_search.
    """

    # All SemanticSearchTools are pure reads.
    default_annotations = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

    def __init__(
        self,
        name: str,
        description: str,
        model: type,
        *,
        embedding_source: str,
        embed_tool_name: str,
        enrich: Callable[[Session, list[dict]], list[dict]] | None = None,
        default_limit: int = 10,
        max_limit: int = 50,
        category: str = "",
        examples: list[str] | None = None,
        annotations: ToolAnnotations | None = None,
    ):
        super().__init__(name, description, model, category=category, examples=examples, annotations=annotations)
        self.embedding_source = embedding_source
        self.embed_tool_name = embed_tool_name
        self.enrich = enrich
        self.default_limit = default_limit
        self.max_limit = max_limit

    def _build_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (default {self.default_limit}, max {self.max_limit}).",
                    "default": self.default_limit,
                },
            },
            "required": ["query"],
        }

    def _make_handler(self) -> Callable[[Session, dict[str, Any]], str]:
        embedding_source = self.embedding_source
        embed_tool_name = self.embed_tool_name
        enrich = self.enrich
        default_limit = self.default_limit
        max_limit = self.max_limit

        def handler(session: Session, arguments: dict[str, Any]) -> str:
            query = arguments.get("query", "").strip()
            if not query:
                return json.dumps({"error": "query is required"})

            top_k = min(int(arguments.get("limit", default_limit)), max_limit)

            from app.services.embedding import Embedding, EmbeddingService

            count = session.query(Embedding).filter_by(source=embedding_source).count()
            if count == 0:
                return json.dumps({
                    "error": f"No {embedding_source} embeddings found. Run {embed_tool_name} first."
                })

            raw_results = EmbeddingService.search(
                session, query=query, sources=[embedding_source], limit=top_k
            )

            if enrich:
                results = enrich(session, raw_results)
            else:
                results = [
                    {
                        "score": r["score"],
                        "source_id": r["source_id"],
                        "preview": r["preview"][:300],
                    }
                    for r in raw_results
                ]

            return json.dumps(results, indent=2)

        return handler

    def build(self) -> dict[str, Any]:
        return self._base_tool(self._build_schema(), self._make_handler())
