"""Tool registry — V4 chunk 2.1.

The single source of truth for "what tools exist and how do I call them",
populated once at startup by `app.mcp.server.register_mcp_tools()`.

Lives here (not in `app/mcp/server.py`) so `app/plugin/dispatch.py` — the one
chokepoint both the MCP and HTTP transports dispatch through — never has to
import the MCP server module (which pulls in the `mcp` SDK, the Streamable
HTTP session manager, etc). `app/mcp/server.py` and `app/api/v1.py` both
import these same dict objects (by reference, not by copy) so mutating one
view mutates the other — existing tests that monkeypatch
`app.mcp.server._tool_handlers` in place keep working unchanged.
"""

from __future__ import annotations

from typing import Any, Callable

from mcp.types import Tool

# tool_name -> (handler_fn, integration_name)
tool_handlers: dict[str, tuple[Callable, str]] = {}

# Full MCP Tool definitions (name, description, inputSchema, annotations),
# in registration order — this is what `list_tools()` returns.
tool_definitions: list[Tool] = []

# tool_name -> {"annotations": ..., "category": ..., "examples": ...}
tool_metadata: dict[str, dict[str, Any]] = {}


def get_tool_handler(name: str) -> tuple[Callable, str] | None:
    """Return `(handler_fn, integration_name)` for `name`, or None if unknown."""
    return tool_handlers.get(name)


def get_tool_annotations(name: str) -> dict[str, Any] | None:
    """Return the MCP annotations registered for `name`, or None if it has none.

    Read through the module attribute at call time (not a captured reference)
    so a test that swaps `tool_metadata` for an isolated copy is seen by the
    dispatcher — the same reason `get_tool_handler` exists.
    """
    return (tool_metadata.get(name) or {}).get("annotations")
