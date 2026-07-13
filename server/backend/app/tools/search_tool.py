"""SearchTool — declarative builder for keyword search tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.tools.base import ExtraFilter, ToolAnnotations, ToolBuilder, parse_iso_date


class SearchTool(ToolBuilder):
    """Generates a keyword search tool: ILIKE across specified text columns.

    Replaces hand-written handlers like gmail_search, whatsapp_search, lastfm_search.
    """

    # All SearchTools are pure reads.
    default_annotations = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

    def __init__(
        self,
        name: str,
        description: str,
        model: type,
        *,
        search_columns: list[str],
        timestamp_col: str,
        to_dict: Callable,
        multi_term: bool = True,
        default_limit: int = 20,
        max_limit: int = 100,
        date_params: tuple[str, str] = ("after", "before"),
        extra_filters: list[ExtraFilter] | None = None,
        category: str = "",
        examples: list[str] | None = None,
        annotations: ToolAnnotations | None = None,
    ):
        super().__init__(name, description, model, category=category, examples=examples, annotations=annotations)
        self.search_columns = search_columns
        self.timestamp_col = timestamp_col
        self.to_dict = to_dict
        self.multi_term = multi_term
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.date_params = date_params
        self.extra_filters = extra_filters or []

    def _build_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "query": {
                "type": "string",
                "description": "Search text.",
            },
        }
        required = ["query"]

        # Date range params
        after_name, before_name = self.date_params
        properties[after_name] = {
            "type": "string",
            "description": f"Only results after this date (ISO format, e.g. 2026-02-15).",
        }
        properties[before_name] = {
            "type": "string",
            "description": f"Only results before this date (ISO format, e.g. 2026-03-30).",
        }

        # Extra filters
        for f in self.extra_filters:
            properties[f.param_name] = f.schema_property()
            if f.is_required:
                required.append(f.param_name)

        properties["limit"] = {
            "type": "integer",
            "description": f"Max results to return (default {self.default_limit}, max {self.max_limit}).",
            "default": self.default_limit,
        }

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def _make_handler(self) -> Callable[[Session, dict[str, Any]], str]:
        model = self.model
        search_columns = self.search_columns
        timestamp_col = self.timestamp_col
        to_dict = self.to_dict
        multi_term = self.multi_term
        default_limit = self.default_limit
        max_limit = self.max_limit
        after_name, before_name = self.date_params
        extra_filters = self.extra_filters

        def handler(session: Session, arguments: dict[str, Any]) -> str:
            search = arguments.get("query", "").strip()
            if not search:
                return json.dumps({"error": "query is required"})

            limit = min(int(arguments.get("limit", default_limit)), max_limit)

            # Build search filter
            terms = search.split() if multi_term else [search]
            term_filters = []
            for term in terms:
                pattern = f"%{term}%"
                col_filters = [getattr(model, col).ilike(pattern) for col in search_columns]
                term_filters.append(or_(*col_filters))

            from app.tools.helpers import scoped_query

            # Auto-scope per-user models to the bearer's user_id.
            query = scoped_query(session, model).filter(or_(*term_filters))

            # Date range
            ts_col = getattr(model, timestamp_col)
            after_val = parse_iso_date(arguments.get(after_name))
            if after_val:
                query = query.filter(ts_col >= after_val)
            before_val = parse_iso_date(arguments.get(before_name))
            if before_val:
                query = query.filter(ts_col <= before_val)

            # Extra filters
            for f in extra_filters:
                val = arguments.get(f.param_name, f.default)
                query = f.apply(session, query, model, val)

            query = query.order_by(ts_col.desc().nullslast())
            rows = query.limit(limit).all()

            return json.dumps([to_dict(r) for r in rows], indent=2)

        return handler

    def build(self) -> dict[str, Any]:
        return self._base_tool(self._build_schema(), self._make_handler())
