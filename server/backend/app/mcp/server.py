"""MCP server — auto-discovers tools from registered integrations.

Exposed via Streamable HTTP transport at /mcp (stateless, JSON responses).

Auth: prefers per-user `client_tokens` bearer (resolved to a User row and
pinned via `app.auth.context.current_user_id` ContextVar for the duration
of each tool call). Falls back to shared `HOME_MCP_TOKEN` for legacy/admin
access — that path defaults to user_id=1 (Alex).

Transport history: SSE (deprecated MCP spec, replaced by Streamable HTTP
2025-03-26). The old SSE path was flaky behind Tailscale because the SDK's
in-memory session-id map drifted from the client's view whenever the SSE
stream was idle-reaped, producing JSON-RPC -32602 on the next POST. Stateless
Streamable HTTP avoids this — each request is a self-contained POST, no
shared session map to lose.
"""

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from typing import Any, Callable

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth.client_token import resolve_token_to_user
from app.auth.context import use_user
from app.config import settings
from app.db import get_db
from app.errors import PermanentError
from app.integrations import get_all
from app.mcp.annotations import TOOL_ANNOTATIONS
from app.mcp.instructions import COMAR_INSTRUCTIONS
from app.models.users import User
from app.services.tool_calls import record_tool_call

logger = logging.getLogger(__name__)

# Ceiling for any single MCP tool call. MCP tools should be fast queries;
# long-running work (backfills, embeds) goes via /api/* endpoints.
MCP_TOOL_TIMEOUT_SECONDS = 60

mcp_server = Server("comar-server", instructions=COMAR_INSTRUCTIONS)

# Stateless Streamable HTTP — no per-session map, JSON responses (no SSE).
# `stateless=True` means a fresh transport per request; intermediaries
# (Tailscale, Caddy) can drop idle TCP and the next call still succeeds.
session_manager = StreamableHTTPSessionManager(
    app=mcp_server,
    stateless=True,
    json_response=True,
)


@contextlib.asynccontextmanager
async def mcp_lifespan() -> AsyncIterator[None]:
    """Lifespan hook: starts/stops the session manager's task group.

    Must be wired into the FastAPI app's lifespan; the manager can only be
    `run()` once per instance.
    """
    async with session_manager.run():
        yield

# Registry: tool_name → (handler_fn, integration_name)
_tool_handlers: dict[str, tuple[Callable, str]] = {}
_tool_definitions: list[Tool] = []
# Extra metadata: tool_name → {"category": str, "examples": list[str]}
_tool_metadata: dict[str, dict[str, Any]] = {}


# Per-connection user context. Set inside mcp_asgi_app before mcp_server.run;
# child Tasks spawned by the MCP SDK (one per incoming JSON-RPC message) inherit
# this ContextVar, so call_tool sees the connection's User without any plumbing
# through the MCP API.
_connection_user: ContextVar[User | None] = ContextVar(
    "mcp_connection_user", default=None,
)

# Per-call correlation id, same ContextVar pattern as `_connection_user` but
# scoped to a single tool invocation rather than a whole connection. Bound in
# `call_tool` (and mirrored in `app/api/v1.py::call_tool` via
# `bind_tool_call_id`) around the dispatch of one tool, so any logging done
# inside a handler during that window can correlate back to the structured
# completion line and the persisted `tool_calls` row via `current_tool_call_id()`.
_tool_call_id: ContextVar[str | None] = ContextVar(
    "mcp_tool_call_id", default=None,
)


def current_tool_call_id() -> str | None:
    """Return the id of the in-flight tool call, or None outside one."""
    return _tool_call_id.get()


@contextlib.contextmanager
def bind_tool_call_id(call_id: str) -> Iterator[None]:
    """Bind `call_id` on `_tool_call_id` for the duration of the with-block."""
    token = _tool_call_id.set(call_id)
    try:
        yield
    finally:
        _tool_call_id.reset(token)


def _authenticate_request(request: Request) -> User | None:
    """Resolve bearer → User, or None if the bearer is the legacy admin token.

    Returns:
        - User instance if bearer matches a row in `client_tokens`
        - User instance representing user_id=1 (Alex) if bearer matches the
          shared `HOME_MCP_TOKEN` (legacy admin path)
        - None if bearer is missing/invalid
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not token:
        return None

    # 1. Per-user client_tokens (preferred path — daemon, Claude Code).
    user = resolve_token_to_user(token)
    if user is not None:
        return user

    # 2. OAuth 2.1 access token (claude.ai web/mobile connectors).
    from app.auth.oauth_provider import resolve_oauth_token_to_user
    oauth_user = resolve_oauth_token_to_user(token)
    if oauth_user is not None:
        return oauth_user

    # 3. Shared HOME_MCP_TOKEN fallback (admin / legacy).
    if settings.mcp_token:
        from app.auth.utils import safe_token_check
        if safe_token_check(token, settings.mcp_token):
            logger.info("MCP: legacy HOME_MCP_TOKEN bearer accepted (admin scope)")
            # Synthesise a minimal User pinning user_id=1 so handlers get a
            # ContextVar value. Admin-scope reads land in Alex's data.
            return User(id=1, name="alex", display_name="Alex (admin token)")

    return None


async def mcp_asgi_app(scope, receive, send):
    """ASGI app mounted at /mcp — Streamable HTTP transport.

    Authenticates each request, pins the user on a ContextVar, then delegates
    to the stateless session manager. Because anyio's task spawning copies
    the current `contextvars.Context`, the per-request user is inherited by
    the inner task that runs the MCP server loop, and `call_tool` sees it.

    The legacy `/sse` path returns 410 Gone with a clear message — that
    transport is removed.
    """
    if scope["type"] != "http":
        # Not expected — Streamable HTTP is HTTP-only. Be loud rather than silent.
        response = JSONResponse(
            {"error": f"Unsupported scope type: {scope['type']}"}, status_code=400
        )
        await response(scope, receive, send)
        return

    raw_path = scope.get("path", "")
    root = scope.get("root_path", "")
    path = raw_path[len(root):] if raw_path.startswith(root) else raw_path

    # Legacy SSE callers — give a clear signal to upgrade.
    if path.startswith("/sse") or path.startswith("/messages"):
        response = JSONResponse(
            {
                "error": "MCP SSE transport removed; use Streamable HTTP at /mcp",
                "spec": "https://modelcontextprotocol.io/specification/2025-03-26/basic/transports",
            },
            status_code=410,
        )
        await response(scope, receive, send)
        return

    request = Request(scope, receive, send)
    user = _authenticate_request(request)
    if user is None:
        response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        await response(scope, receive, send)
        return

    token = _connection_user.set(user)
    try:
        await session_manager.handle_request(scope, receive, send)
    finally:
        _connection_user.reset(token)


def register_mcp_tools() -> None:
    """Collect MCP tools from all integrations and register handlers."""
    integrations = get_all()

    for integration in integrations.values():
        if not integration.is_configured():
            logger.info(f"Skipping {integration.name} — not configured")
            continue

        try:
            tools = integration.mcp_tools()
            count = 0
            for tool_def in tools:
                name = tool_def["name"]
                handler = tool_def.get("handler")
                if handler is None:
                    logger.warning(f"Tool {name} has no handler — skipping")
                    continue

                _tool_handlers[name] = (handler, integration.name)
                # Resolve annotations: explicit on tool dict wins; else centralized fallback.
                annotations = tool_def.get("annotations") or TOOL_ANNOTATIONS.get(name)
                tool_kwargs: dict[str, Any] = {
                    "name": name,
                    "description": tool_def.get("description", ""),
                    "inputSchema": tool_def.get("inputSchema", {"type": "object", "properties": {}}),
                }
                if annotations:
                    # MCP SDK accepts annotations as a dict; pass through verbatim.
                    tool_kwargs["annotations"] = annotations
                _tool_definitions.append(Tool(**tool_kwargs))
                # Store optional metadata (category, examples, annotations) for HTTP /tools
                meta = {}
                if "category" in tool_def:
                    meta["category"] = tool_def["category"]
                if "examples" in tool_def:
                    meta["examples"] = tool_def["examples"]
                if annotations:
                    meta["annotations"] = annotations
                if meta:
                    _tool_metadata[name] = meta
                count += 1
            logger.info(f"  {integration.name}: {count} tools registered")
        except Exception:
            logger.exception(f"Failed to register tools for {integration.name} — skipping")

    # Register cross-source embedding tools (not tied to any integration)
    _register_embedding_tools()

    tool_names = sorted(_tool_handlers.keys())
    logger.info(f"Registered {len(_tool_handlers)} MCP tools from {len(integrations)} integrations")
    logger.debug(f"Tool names: {tool_names}")


def _register_embedding_tools() -> None:
    """Register the cross-source semantic search and stats tools."""
    from app.services.embedding import EmbeddingService

    def handle_semantic_search(session, arguments):
        query = arguments.get("query", "").strip()
        if not query:
            return json.dumps({"error": "query is required"})

        sources = arguments.get("sources")  # None = all, or list like ["vault", "email"]
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.split(",")]
        limit = min(int(arguments.get("limit", 10)), 50)

        results = EmbeddingService.search(session, query=query, sources=sources, limit=limit)
        return json.dumps(results, indent=2)

    def handle_embedding_stats(session, arguments):
        stats = EmbeddingService.stats(session)
        return json.dumps(stats, indent=2)

    _tool_handlers["search_semantic"] = (handle_semantic_search, "embedding")
    _tool_metadata["search_semantic"] = {"annotations": {"readOnlyHint": True, "idempotentHint": True}}
    _tool_definitions.append(Tool(
        name="search_semantic",
        description=(
            "Cross-source semantic search across all embedded content — vault notes, "
            "emails, and WhatsApp messages. Finds content by meaning, not just keywords. "
            "Use this for broad searches when you don't know which source has the answer. "
            "For source-specific search, use vault_search, gmail_semantic_search, or "
            "whatsapp_semantic_search instead."
        ),
        annotations={"readOnlyHint": True, "idempotentHint": True},
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query.",
                },
                "sources": {
                    "type": "string",
                    "description": "Comma-separated sources to search (e.g. 'vault,email,whatsapp'). Omit for all sources.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, max 50).",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    ))

    _tool_handlers["search_stats"] = (handle_embedding_stats, "embedding")
    _tool_metadata["search_stats"] = {"annotations": {"readOnlyHint": True, "idempotentHint": True}}
    _tool_definitions.append(Tool(
        name="search_stats",
        description=(
            "Unified embedding pipeline statistics: total embeddings per source "
            "(vault, email, WhatsApp), queue status, and model info. "
            "Admin tool for checking search index health."
        ),
        annotations={"readOnlyHint": True, "idempotentHint": True},
        inputSchema={"type": "object", "properties": {}},
    ))

    # Register the list_tools handler
    @mcp_server.list_tools()
    async def list_tools() -> list[Tool]:
        return _tool_definitions

    # Register the call_tool dispatcher
    @mcp_server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        if name not in _tool_handlers:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

        handler_fn, integration_name = _tool_handlers[name]
        arguments = arguments or {}

        # Pin per-user scope for the duration of this tool call. The
        # connection-level _connection_user ContextVar was set when the SSE
        # client authenticated; if it's missing we refuse the call rather
        # than silently default to Alex — that was a cross-user data leak.
        user = _connection_user.get()
        if user is None:
            return [TextContent(type="text", text=json.dumps({
                "error": "Unauthorized: MCP call has no authenticated user"
            }))]
        user_id = user.id

        call_id = secrets.token_hex(4)
        start = time.monotonic()
        status = "ok"
        error_text: str | None = None
        with bind_tool_call_id(call_id):
            try:
                def _run():
                    from app.services.freshness import ensure_fresh
                    db = get_db()
                    with db.session() as session, use_user(user_id):
                        ensure_fresh(integration_name, session)
                        return handler_fn(session, arguments)

                result = await asyncio.wait_for(
                    asyncio.to_thread(_run), timeout=MCP_TOOL_TIMEOUT_SECONDS
                )
                if not isinstance(result, str):
                    result = json.dumps(result)
                return [TextContent(type="text", text=result)]
            except asyncio.TimeoutError:
                status = "timeout"
                error_text = f"Tool {name} timed out after {MCP_TOOL_TIMEOUT_SECONDS}s"
                logger.warning(f"Tool {name} timed out after {MCP_TOOL_TIMEOUT_SECONDS}s")
                return [TextContent(type="text", text=json.dumps({"error": error_text}))]
            except PermanentError as e:
                # Bad credentials / config, not a bug worth a stack trace every
                # call — warn (no traceback spam) and return a structured error.
                status = "error"
                error_text = str(e)
                logger.warning(f"Tool {name} failed (permanent): {e}")
                return [TextContent(type="text", text=json.dumps({"error": error_text}))]
            except Exception as e:
                status = "error"
                error_text = str(e)
                logger.exception(f"Tool {name} failed")
                return [TextContent(type="text", text=json.dumps({"error": error_text}))]
            finally:
                duration_ms = int((time.monotonic() - start) * 1000)
                logger.info(
                    "tool=%s user=%s tool_call_id=%s duration_ms=%d status=%s",
                    name, user_id, call_id, duration_ms, status,
                )
                await asyncio.to_thread(
                    record_tool_call,
                    name=name, user_id=user_id, duration_ms=duration_ms,
                    status=status, error=error_text, tool_call_id=call_id,
                )
