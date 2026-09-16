"""MCP tool definitions for the `_template` scaffold integration — V4 chunk 4.3e.

Two tools, demonstrating the two DSL builders you'll reach for most often
(see `server/docs/writing-an-integration.md`'s "DSL tools + annotations"
section for the full builder list — `ListTool`, `SearchTool`,
`SemanticSearchTool`, `StatsTool`, `CustomTool`):

  - `template_list_items` — `ListTool`: the mechanical "list rows in a date
    range, ordered, limited" shape. `ListTool` auto-scopes per-user models
    (via `app.tools.helpers.scoped_query`, since `TemplateItem` carries
    `UserOwnedMixin`) and sets sensible read-only annotations for you — you
    only write `to_dict`.
  - `template_ping` — `CustomTool`: wraps a hand-written handler that
    doesn't fit any mechanical shape. Every `CustomTool` MUST pass explicit
    `annotations` — there's no default, because a hand-written handler's
    behaviour (read vs write vs destructive) can't be inferred.

Every tool, from either builder, ends up carrying inline MCP `annotations` —
`app.mcp.server.register_mcp_tools()` raises `MissingAnnotationsError` at
startup for any tool that doesn't (V4 chunk 1.2's "no centralized annotation
fallback" rule). This is the #1 thing a startup-validation test in
`test_drop_in_integration.py`'s "deliberately broken variants" checks for.

Relative import below (`.models`) — see `__init__.py`'s module docstring for
why this scaffold uses relative intra-package imports instead of the
codebase's usual absolute convention.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from .models import TemplateItem
from app.tools import CustomTool, ListTool, ToolAnnotations


def _item_to_dict(item: TemplateItem) -> dict[str, Any]:
    return {
        "external_id": item.external_id,
        "title": item.title,
        "fetched_at": item.fetched_at.isoformat(),
    }


def handle_ping(session: Session, arguments: dict[str, Any]) -> str:
    """Trivial hand-written handler — a liveness/smoke-test tool.

    Real integrations use `CustomTool` for handlers that genuinely don't fit
    ListTool/SearchTool/SemanticSearchTool/StatsTool's shapes (e.g.
    google_calendar's `calendar_today` — a bespoke "midnight-to-midnight in
    a fixed timezone" query, or `calendar_create_event` — a write against an
    external API). This one is deliberately trivial so the scaffold has a
    second, distinct tool to exercise `CustomTool` without adding real
    behaviour to teach around.
    """
    return json.dumps({"status": "ok", "integration": "__TEMPLATE_INTEGRATION_NAME__"})


def get_mcp_tools() -> list[dict[str, Any]]:
    """Return this integration's MCP tool definitions. Called by
    `TemplateIntegration.mcp_tools()` in `__init__.py` — never called
    directly by the kernel (`BaseIntegration.mcp_tools()` is each
    integration's own entrypoint, collected by
    `app.mcp.server.register_mcp_tools()`).
    """
    return [
        ListTool(
            name="template_list_items",
            description="List recently synced template items for the current user.",
            model=TemplateItem,
            timestamp_col="fetched_at",
            to_dict=_item_to_dict,
            default_limit=20,
            max_limit=100,
            category="template",
            examples=["Show my recent template items"],
        ).build(),
        CustomTool(
            name="template_ping",
            description="Liveness check for the template integration — returns a fixed OK payload.",
            input_schema={"type": "object", "properties": {}},
            handler=handle_ping,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="template",
            examples=["Is the template integration alive?"],
        ).build(),
    ]
