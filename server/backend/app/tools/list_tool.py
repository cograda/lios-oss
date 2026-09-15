"""ListTool — declarative builder for list/recent tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.tools.base import ExtraFilter, ToolAnnotations, ToolBuilder, parse_iso_date


class ListTool(ToolBuilder):
    """Generates a list/recent tool: query table + date range + extra filters + order + limit + format.

    Replaces hand-written handlers like lastfm_recent, gmail_recent, whatsapp_recent,
    calendar_list_events — all of which follow the same pattern.
    """

    # All ListTools are pure reads.
    default_annotations = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

    def __init__(
        self,
        name: str,
        description: str,
        model: type,
        *,
        timestamp_col: str,
        to_dict: Callable,
        default_limit: int = 20,
        max_limit: int = 100,
        default_order: str = "desc",
        date_params: tuple[str, str] = ("after", "before"),
        extra_filters: list[ExtraFilter] | None = None,
        post_process: Callable[[list[dict]], list[dict]] | None = None,
        scope_filter: Callable[[Session, Any], Any] | None = None,
        category: str = "",
        examples: list[str] | None = None,
        annotations: ToolAnnotations | None = None,
    ):
        super().__init__(name, description, model, category=category, examples=examples, annotations=annotations)
        self.timestamp_col = timestamp_col
        self.to_dict = to_dict
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.default_order = default_order
        self.date_params = date_params
        self.extra_filters = extra_filters or []
        self.post_process = post_process
        # `(session, query) -> query`, applied unconditionally right after the
        # per-user scoping — unlike an ExtraFilter, which is skipped when its
        # argument is absent. Exists for restrictions that must hold whether
        # or not the caller asked for a filter: obsidian's folder-scoped grant
        # uses it so `vault_recent` with no `folder` argument still cannot
        # list paths outside the grant.
        self.scope_filter = scope_filter

    def _build_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []

        # Date range params
        after_name, before_name = self.date_params
        properties[after_name] = {
            "type": "string",
            "description": f"Start date (ISO format, e.g. 2026-03-01). Optional.",
        }
        properties[before_name] = {
            "type": "string",
            "description": f"End date (ISO format, e.g. 2026-03-26). Optional.",
        }

        # Extra filters
        for f in self.extra_filters:
            properties[f.param_name] = f.schema_property()
            if f.is_required:
                required.append(f.param_name)

        # Limit — accept int or stringified int (some clients coerce to string)
        properties["limit"] = {
            "type": ["integer", "string"],
            "description": f"Max results to return (default {self.default_limit}, max {self.max_limit}).",
            "default": self.default_limit,
        }

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

    def _make_handler(self) -> Callable[[Session, dict[str, Any]], str]:
        # Capture all config in closure
        model = self.model
        timestamp_col = self.timestamp_col
        to_dict = self.to_dict
        default_limit = self.default_limit
        max_limit = self.max_limit
        default_order = self.default_order
        after_name, before_name = self.date_params
        extra_filters = self.extra_filters
        post_process = self.post_process
        scope_filter = self.scope_filter

        def handler(session: Session, arguments: dict[str, Any]) -> str:
            limit = min(int(arguments.get("limit", default_limit)), max_limit)

            from app.tools.helpers import scoped_query

            # Auto-scope per-user models: any model carrying UserOwnedMixin
            # (i.e. has a user_id column) is filtered to the bearer's user_id.
            query = scoped_query(session, model)
            if scope_filter is not None:
                query = scope_filter(session, query)

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

            # Order
            if default_order == "desc":
                query = query.order_by(ts_col.desc())
            else:
                query = query.order_by(ts_col.asc())

            rows = query.limit(limit).all()
            results = [to_dict(r) for r in rows]

            if post_process:
                results = post_process(results)

            return json.dumps(results, indent=2)

        return handler

    def build(self) -> dict[str, Any]:
        return self._base_tool(self._build_schema(), self._make_handler())
