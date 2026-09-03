"""SemanticSearchTool — declarative builder for embedding-backed search tools."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.tools.base import ToolAnnotations, ToolBuilder, parse_iso_date


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
        date_filter: Callable[[datetime | None, datetime | None], Any | None] | None = None,
        date_params: tuple[str, str] = ("after", "before"),
    ):
        super().__init__(name, description, model, category=category, examples=examples, annotations=annotations)
        self.embedding_source = embedding_source
        self.embed_tool_name = embed_tool_name
        self.enrich = enrich
        self.default_limit = default_limit
        self.max_limit = max_limit
        # `date_filter`, when supplied, turns parsed after/before datetimes into
        # a SQLAlchemy filter clause against `Embedding` — the shape of "date"
        # differs per source (mail has a sent date, whatsapp a message
        # timestamp), so this class stays agnostic of that and just plumbs the
        # parsed bounds to whichever integration configured it. `None` means
        # this tool doesn't support date filtering at all (schema omits the
        # params), matching every existing SemanticSearchTool call site
        # unchanged.
        self.date_filter = date_filter
        self.date_params = date_params

    def _build_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "query": {
                "type": "string",
                "description": "Natural language search query.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max results (default {self.default_limit}, max {self.max_limit}).",
                "default": self.default_limit,
            },
        }

        if self.date_filter is not None:
            after_name, before_name = self.date_params
            properties[after_name] = {
                "type": "string",
                "description": "Only include results dated on or after this date (ISO format, e.g. 2026-03-01). Optional.",
            }
            properties[before_name] = {
                "type": "string",
                "description": "Only include results dated on or before this date (ISO format, e.g. 2026-03-26). Optional.",
            }

        return {
            "type": "object",
            "properties": properties,
            "required": ["query"],
        }

    def _make_handler(self) -> Callable[[Session, dict[str, Any]], str]:
        embedding_source = self.embedding_source
        embed_tool_name = self.embed_tool_name
        enrich = self.enrich
        default_limit = self.default_limit
        max_limit = self.max_limit
        date_filter = self.date_filter
        after_name, before_name = self.date_params

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

            extra_filter = None
            date_range_requested = False
            if date_filter is not None:
                after_val = parse_iso_date(arguments.get(after_name))
                before_val = parse_iso_date(arguments.get(before_name))
                date_range_requested = after_val is not None or before_val is not None
                if date_range_requested:
                    extra_filter = date_filter(after_val, before_val)

            raw_results = EmbeddingService.search(
                session, query=query, sources=[embedding_source], limit=top_k,
                extra_filter=extra_filter,
            )

            if not raw_results and date_range_requested:
                # Deliberately `note`, not `error`. Filtering to nothing is a
                # legitimate outcome, and the success path returns a bare list —
                # a caller branching on "error" in the payload would read this
                # as a failure. The note exists so the caller can say *why* it
                # found nothing instead of guessing whether the filter or the
                # query was responsible.
                return json.dumps({
                    "results": [],
                    "note": (
                        f"No {embedding_source} results in that date range "
                        f"({after_name}={arguments.get(after_name)!r}, "
                        f"{before_name}={arguments.get(before_name)!r}). "
                        "Try widening or omitting the date range."
                    ),
                })

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
