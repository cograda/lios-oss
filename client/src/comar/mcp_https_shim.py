"""MCP tool proxy — forwards tool calls to comar-server via HTTPS.

Each proxied tool is registered on the local MCP server. When Claude Code
invokes it, the handler calls ServerClient.call_tool() over HTTP, then
returns the JSON result as TextContent.

Replaces the old gRPC-based mcp_proxy.py with a one-line transport swap.
"""

import logging
from typing import Any

from mcp.types import TextContent, Tool

from comar.server_client import ServerClient

logger = logging.getLogger(__name__)


def register_proxied_tools(
    tool_definitions: list[dict],
    server_client: ServerClient,
) -> tuple[list[Tool], dict[str, Any]]:
    """Build MCP Tool objects and handler map for server-proxied tools."""
    tools: list[Tool] = []
    handlers: dict[str, Any] = {}

    for defn in tool_definitions:
        name = defn["name"]
        schema = defn.get("inputSchema", {"type": "object", "properties": {}})
        if isinstance(schema, dict) and "required" not in schema:
            schema = {**schema, "required": []}
        tools.append(Tool(
            name=name,
            description=defn.get("description", ""),
            inputSchema=schema,
        ))
        handlers[name] = _make_proxy_handler(name, server_client)

    logger.info("Registered %d proxied server tools", len(tools))
    return tools, handlers


def _make_proxy_handler(tool_name: str, server_client: ServerClient):
    def handler(arguments: dict[str, Any] | None) -> list[TextContent]:
        arguments = arguments or {}
        result_json = server_client.call_tool(tool_name, arguments)
        return [TextContent(type="text", text=result_json)]
    return handler
