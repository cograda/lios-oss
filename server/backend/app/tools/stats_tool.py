"""StatsTool — declarative builder for stats/summary tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.tools.base import ToolAnnotations, ToolBuilder


class StatsTool(ToolBuilder):
    """Generates a stats tool with a compute callback.

    Stats queries are too varied to fully generalize (lastfm has period filtering +
    top artists + genre joins; gmail has per-account breakdown; whatsapp has
    group/contact splits). The compute callback keeps the domain logic explicit
    while registering through the unified DSL.
    """

    # All StatsTools are pure reads.
    default_annotations = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

    def __init__(
        self,
        name: str,
        description: str,
        model: type,
        *,
        compute: Callable[[Session, dict[str, Any]], dict],
        input_schema: dict[str, Any] | None = None,
        category: str = "",
        examples: list[str] | None = None,
        annotations: ToolAnnotations | None = None,
    ):
        super().__init__(name, description, model, category=category, examples=examples, annotations=annotations)
        self.compute = compute
        self.input_schema = input_schema or {"type": "object", "properties": {}}

    def _make_handler(self) -> Callable[[Session, dict[str, Any]], str]:
        compute = self.compute

        def handler(session: Session, arguments: dict[str, Any]) -> str:
            result = compute(session, arguments)
            return json.dumps(result, indent=2)

        return handler

    def build(self) -> dict[str, Any]:
        return self._base_tool(self.input_schema, self._make_handler())
