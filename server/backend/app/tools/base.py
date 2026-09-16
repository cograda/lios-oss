"""Base classes for the declarative tool DSL."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike


def parse_iso_date(value: str | None) -> datetime | None:
    """Parse an ISO date string, returning None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@dataclass
class ExtraFilter:
    """Declares an extra filter parameter on a tool.

    For simple cases, match_mode is "exact" or "ilike".
    For complex cases (e.g., WhatsApp chat lookup via join),
    pass a callable: (session, query, value) -> query.
    """

    param_name: str
    column: str
    description: str
    param_type: str = "string"
    is_required: bool = False
    match_mode: str | Callable = "exact"  # "exact", "ilike", or callable
    default: Any = None
    enum: list[str] | None = None

    def schema_property(self) -> dict[str, Any]:
        """Generate the JSON Schema property for this filter."""
        prop: dict[str, Any] = {
            "type": self.param_type,
            "description": self.description,
        }
        if self.default is not None:
            prop["default"] = self.default
        if self.enum is not None:
            prop["enum"] = self.enum
        return prop

    def apply(self, session: Session, query: Any, model: type, value: Any) -> Any:
        """Apply this filter to a SQLAlchemy query."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return query

        if callable(self.match_mode):
            return self.match_mode(session, query, value)

        col = getattr(model, self.column)
        if self.match_mode == "ilike":
            return query.filter(
                col.ilike(f"%{escape_ilike(value)}%", escape=ILIKE_ESCAPE_CHAR)
            )
        else:  # exact
            return query.filter(col == value)


@dataclass
class ToolAnnotations:
    """MCP tool annotations — hints to the client about how a tool behaves.

    These are advisory: clients use them to label tools, decide which
    require user confirmation, and explain capabilities to the model.
    All fields default to None (omitted from the wire) — set explicitly.

    Reference: https://modelcontextprotocol.io/specification (Tool annotations)
    """

    title: str | None = None
    """Human-readable label, e.g. 'Search emails'. Falls back to the tool name."""

    read_only_hint: bool | None = None
    """True if the tool only reads — never mutates state. Lets clients skip confirmation."""

    destructive_hint: bool | None = None
    """True if the tool may delete or destroy data (only set when read_only_hint is False)."""

    idempotent_hint: bool | None = None
    """True if calling the tool repeatedly with the same args has no extra effect."""

    open_world_hint: bool | None = None
    """True if the tool reaches outside the local system (network, third-party APIs)."""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.title is not None:
            out["title"] = self.title
        if self.read_only_hint is not None:
            out["readOnlyHint"] = self.read_only_hint
        if self.destructive_hint is not None:
            out["destructiveHint"] = self.destructive_hint
        if self.idempotent_hint is not None:
            out["idempotentHint"] = self.idempotent_hint
        if self.open_world_hint is not None:
            out["openWorldHint"] = self.open_world_hint
        return out


class ToolBuilder(ABC):
    """Base class for declarative tool builders."""

    # Subclasses override to provide sensible defaults (e.g. ListTool always reads).
    default_annotations: ToolAnnotations = ToolAnnotations()

    def __init__(
        self,
        name: str,
        description: str,
        model: type,
        *,
        category: str = "",
        examples: list[str] | None = None,
        annotations: ToolAnnotations | None = None,
    ):
        self.name = name
        self.description = description
        self.model = model
        self.category = category
        self.examples = examples or []
        # Merge: explicit annotations override defaults; missing fields fall through.
        self.annotations = self._merge_annotations(annotations)

    def _merge_annotations(self, override: ToolAnnotations | None) -> ToolAnnotations:
        if override is None:
            return self.default_annotations
        return ToolAnnotations(
            title=override.title if override.title is not None else self.default_annotations.title,
            read_only_hint=override.read_only_hint if override.read_only_hint is not None else self.default_annotations.read_only_hint,
            destructive_hint=override.destructive_hint if override.destructive_hint is not None else self.default_annotations.destructive_hint,
            idempotent_hint=override.idempotent_hint if override.idempotent_hint is not None else self.default_annotations.idempotent_hint,
            open_world_hint=override.open_world_hint if override.open_world_hint is not None else self.default_annotations.open_world_hint,
        )

    @abstractmethod
    def build(self) -> dict[str, Any]:
        """Return the MCP tool definition dict."""
        ...

    def _base_tool(self, input_schema: dict, handler: Callable) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": input_schema,
            "handler": handler,
        }
        if self.category:
            tool["category"] = self.category
        if self.examples:
            tool["examples"] = self.examples
        ann_dict = self.annotations.to_dict()
        if ann_dict:
            tool["annotations"] = ann_dict
        return tool


class CustomTool(ToolBuilder):
    """Wraps an existing hand-written handler into the DSL registration flow.

    No default annotations — caller MUST pass explicit hints because behaviour
    varies (a `*_search` is read-only; a `*_create` is destructive). Forces
    every wrapper to declare intent.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[[Session, dict[str, Any]], str],
        *,
        category: str = "",
        examples: list[str] | None = None,
        annotations: ToolAnnotations | None = None,
    ):
        # CustomTool doesn't need a model
        super().__init__(name, description, type(None), category=category, examples=examples, annotations=annotations)
        self.input_schema = input_schema
        self.handler = handler

    def build(self) -> dict[str, Any]:
        return self._base_tool(self.input_schema, self.handler)
