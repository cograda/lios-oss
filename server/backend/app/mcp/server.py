"""MCP server — auto-discovers tools from registered integrations.

Exposed via Streamable HTTP transport at /mcp (stateless, JSON responses).

Auth: per-user `client_tokens` bearer or an OAuth 2.1 access token (resolved
to a User row and pinned via `app.auth.context.current_user_id` ContextVar
for the duration of each tool call). The shared `HOME_MCP_TOKEN` admin
fallback was removed in V4 chunk 2.3 — see `_authenticate_request` below.

Transport history: SSE (deprecated MCP spec, replaced by Streamable HTTP
2025-03-26). The old SSE path was flaky behind Tailscale because the SDK's
in-memory session-id map drifted from the client's view whenever the SSE
stream was idle-reaped, producing JSON-RPC -32602 on the next POST. Stateless
Streamable HTTP avoids this — each request is a self-contained POST, no
shared session map to lose.
"""

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth.client_token import resolve_token_to_user
from app.auth.hashing import token_last4
from app.auth.rate_limit import is_over_limit
from app.plugin.dispatch import (  # noqa: F401 — re-exported for existing callers/tests
    bind_tool_call_id,
    current_tool_call_id,
    dispatch_tool,
)
from app.plugin.registry import tool_definitions as _tool_definitions
from app.plugin.registry import tool_handlers as _tool_handlers
from app.plugin.registry import tool_metadata as _tool_metadata


class MissingAnnotationsError(RuntimeError):
    """Raised at MCP tool registration when a tool has no inline annotations.

    Every tool must declare its own `annotations` dict now (V4 chunk 1.2) —
    the centralized `app/mcp/annotations.py` fallback is gone. This must
    propagate out of `register_mcp_tools()` and fail server startup; it is
    deliberately NOT caught by the per-integration `except Exception` below
    (that one is for genuine per-integration registration bugs that
    shouldn't take down the whole server).
    """
from app.integrations import get_all
from app.mcp.instructions import COMAR_INSTRUCTIONS
from app.models.users import User

logger = logging.getLogger(__name__)

# Ceiling for any single MCP tool call. MCP tools should be fast queries;
# long-running work (backfills, embeds) goes via /api/* endpoints. Kept as
# its own module attribute (rather than just using
# `app.plugin.dispatch.TOOL_TIMEOUT_SECONDS` directly) because
# tests/test_mcp_transport.py monkeypatches this specific attribute to force
# a fast timeout in tests; the value is passed into `dispatch_tool()`
# explicitly below so that keeps working.
MCP_TOOL_TIMEOUT_SECONDS = 60

mcp_server = Server("lios", instructions=COMAR_INSTRUCTIONS)

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

# Registry (tool_name -> (handler_fn, integration_name), tool definitions,
# metadata) now lives in `app.plugin.registry` — imported above by reference
# (same dict/list objects) so `app.plugin.dispatch` doesn't need to import
# this module, and code here that mutates `_tool_handlers` etc. (including
# tests that monkeypatch them) still mutates the one shared registry.

# Per-connection user context. Set inside mcp_asgi_app before mcp_server.run;
# child Tasks spawned by the MCP SDK (one per incoming JSON-RPC message) inherit
# this ContextVar, so call_tool sees the connection's User without any plumbing
# through the MCP API.
_connection_user: ContextVar[User | None] = ContextVar(
    "mcp_connection_user", default=None,
)

# Same mechanism, for the connecting client's source IP (V4 chunk 2.5 audit
# trail) — set alongside `_connection_user` in `mcp_asgi_app`, read back by
# `call_tool` when it calls `dispatch_tool()`.
_connection_source_ip: ContextVar[str | None] = ContextVar(
    "mcp_connection_source_ip", default=None,
)

# `bind_tool_call_id` / `current_tool_call_id` now live in
# `app.plugin.dispatch` (the single dispatch chokepoint owns the per-call
# correlation id) — imported above and re-exported under these same names
# for back-compat with existing tests/callers.


class RateLimited(Exception):
    """Raised by `_authenticate_request` when the caller's IP is over the F8
    limit — `mcp_asgi_app` turns this into a 429, never a 401."""


def _authenticate_request(request: Request) -> User | None:
    """Resolve bearer → User, or None if missing/invalid/expired.

    Returns:
        - User instance if bearer matches an active row in `client_tokens`
        - User instance if bearer matches a valid, unrevoked OAuth 2.1
          access token (claude.ai web/mobile connectors)
        - None otherwise

    Raises `RateLimited` (F8) if this source IP is over the per-IP sliding
    window, BEFORE any token parsing/DB lookup or `auth_events` write.

    The shared `HOME_MCP_TOKEN` admin fallback (synthesised user_id=1 for
    any bearer matching a single env-var secret) was removed in V4 chunk
    2.3 — it was a standing backdoor into Alex's data. Every caller now
    needs a real per-user `client_tokens` row or OAuth token.

    Every failure path logs exactly once, with a distinct message for
    "expired" vs "invalid/unknown" so ops can tell the difference — never
    the full token value or request body.
    """
    from app.services.auth_events import record_auth_event

    client_ip = request.client.host if request.client else "unknown"
    # F8: failure-budget check only — 401s spend budget inside
    # record_auth_event, successful auths never do.
    if is_over_limit(client_ip):
        raise RateLimited()

    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not token:
        record_auth_event(outcome="401", source_ip=client_ip, transport="mcp")
        return None

    # 1. Per-user client_tokens (preferred path — daemon, Claude Code).
    from app.auth.client_token import TokenExpiredError as ClientTokenExpiredError
    try:
        user = resolve_token_to_user(token)
    except ClientTokenExpiredError:
        logger.warning(
            "MCP auth failed (client_token expired): last4=%s ip=%s",
            token_last4(token), client_ip,
        )
        record_auth_event(
            outcome="401", token_last4=token_last4(token),
            source_ip=client_ip, transport="mcp",
        )
        return None
    if user is not None:
        return user

    # 2. OAuth 2.1 access token (claude.ai web/mobile connectors).
    from app.auth.oauth_provider import TokenExpiredError as OAuthTokenExpiredError
    from app.auth.oauth_provider import resolve_oauth_token_to_user
    try:
        oauth_user = resolve_oauth_token_to_user(token)
    except OAuthTokenExpiredError:
        logger.warning(
            "MCP auth failed (oauth token expired): last4=%s ip=%s",
            token_last4(token), client_ip,
        )
        record_auth_event(
            outcome="401", token_last4=token_last4(token),
            source_ip=client_ip, transport="mcp",
        )
        return None
    if oauth_user is not None:
        return oauth_user

    logger.warning(
        "MCP auth failed (invalid or unknown bearer): last4=%s ip=%s",
        token_last4(token), client_ip,
    )
    record_auth_event(
        outcome="401", token_last4=token_last4(token),
        source_ip=client_ip, transport="mcp",
    )
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
    try:
        user = _authenticate_request(request)
    except RateLimited:
        response = JSONResponse(
            {"error": "Too many auth attempts — slow down"}, status_code=429,
        )
        await response(scope, receive, send)
        return
    if user is None:
        response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        await response(scope, receive, send)
        return

    token = _connection_user.set(user)
    ip_token = _connection_source_ip.set(
        request.client.host if request.client else None
    )
    try:
        await session_manager.handle_request(scope, receive, send)
    finally:
        _connection_user.reset(token)
        _connection_source_ip.reset(ip_token)



def _with_as_user(
    schema: dict[str, Any], *, integration_name: str, annotations: Any,
) -> dict[str, Any]:
    """Add the `as_user` property to a tool schema, if grants can apply to it.

    Returns the schema unchanged for anything outside a grantable scope or not
    declaring itself read-only — the same two conditions
    `app.services.vault_grants.resolve_effective_user_id` enforces at call time.
    """
    from app.models.vault_grants import GRANTABLE_SCOPES
    from app.services.vault_grants import AS_USER_ARG, _is_read_only

    if integration_name not in GRANTABLE_SCOPES or not _is_read_only(annotations):
        return schema
    if not isinstance(schema, dict):
        return schema

    props = dict(schema.get("properties") or {})
    if AS_USER_ARG in props:
        return schema
    props[AS_USER_ARG] = {
        "type": "string",
        "description": (
            "Run this read-only query against another user's vault instead of "
            "your own, e.g. 'alex'. Requires an existing grant; refused "
            "otherwise rather than silently falling back to your own vault. "
            "A grant may cover only certain folders, in which case results "
            "are limited to them and a `folder` outside them is refused. "
            "Omit it for your own data."
        ),
    }
    return {**schema, "properties": props}


def register_mcp_tools() -> None:
    """Collect MCP tools from all integrations and register handlers."""
    from app.plugin.config_store import is_integration_enabled

    integrations = get_all()

    for integration in integrations.values():
        if not is_integration_enabled(integration.name):
            logger.info(f"Skipping {integration.name} — disabled")
            continue
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

                # Every tool must carry its own inline annotations now (V4
                # chunk 1.2) — the centralized app/mcp/annotations.py fallback
                # is gone. Fail loud at startup rather than silently serving
                # an unhinted tool.
                annotations = tool_def.get("annotations")
                if not annotations:
                    raise MissingAnnotationsError(
                        f"Tool {name!r} (integration {integration.name!r}) has no "
                        f"'annotations' — every MCP tool must declare inline "
                        f"annotations (see app/tools/base.py::ToolAnnotations)."
                    )

                _tool_handlers[name] = (handler, integration.name)
                # Advertise `as_user` on exactly the tools dispatch will accept
                # it for, computed from the same two facts dispatch checks — the
                # integration's scope and the tool's own readOnlyHint. Deriving
                # the schema from the enforcement inputs (rather than listing
                # tool names again here) is what keeps the documented surface
                # and the enforced surface from drifting apart.
                input_schema = _with_as_user(
                    tool_def.get("inputSchema", {"type": "object", "properties": {}}),
                    integration_name=integration.name,
                    annotations=annotations,
                )
                tool_kwargs: dict[str, Any] = {
                    "name": name,
                    "description": tool_def.get("description", ""),
                    "inputSchema": input_schema,
                    # MCP SDK accepts annotations as a dict; pass through verbatim.
                    "annotations": annotations,
                }
                _tool_definitions.append(Tool(**tool_kwargs))
                # Store optional metadata (category, examples, annotations) for HTTP /tools
                meta: dict[str, Any] = {"annotations": annotations}
                if "category" in tool_def:
                    meta["category"] = tool_def["category"]
                if "examples" in tool_def:
                    meta["examples"] = tool_def["examples"]
                _tool_metadata[name] = meta
                count += 1
            logger.info(f"  {integration.name}: {count} tools registered")
        except MissingAnnotationsError:
            raise
        except Exception:
            logger.exception(f"Failed to register tools for {integration.name} — skipping")

    # Cross-source search_semantic/search_stats tools used to be registered
    # here directly, under a synthetic "embedding" integration name that had
    # no package or manifest of its own. V4 chunk 3.4 turned `embedding` into
    # a real capability package (`app.integrations.embedding`) whose
    # `mcp_tools()` returns those same two tool defs — the loop above now
    # picks them up like any other integration's tools, nothing
    # embedding-specific left to do here.
    _register_mcp_handlers()

    tool_names = sorted(_tool_handlers.keys())
    logger.info(f"Registered {len(_tool_handlers)} MCP tools from {len(integrations)} integrations")
    logger.debug(f"Tool names: {tool_names}")


def _register_mcp_handlers() -> None:
    """Register the MCP `list_tools`/`call_tool` handlers on `mcp_server`.

    Not tied to any one integration — this just wires the shared
    `_tool_definitions`/dispatch machinery into the MCP SDK's server object,
    once, after every integration's tools have been collected above.
    """

    @mcp_server.list_tools()
    async def list_tools() -> list[Tool]:
        return _tool_definitions

    # Register the call_tool dispatcher — a thin adapter over the shared
    # `dispatch_tool()` chokepoint (V4 chunk 2.1). Auth is resolved here
    # (transport-specific: the connection-level ContextVar set by
    # `mcp_asgi_app`), never inside `dispatch_tool` itself.
    @mcp_server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        # Pin per-user scope for the duration of this tool call. The
        # connection-level _connection_user ContextVar was set when the
        # client authenticated; if it's missing we refuse the call rather
        # than silently default to Alex — that was a cross-user data leak.
        user = _connection_user.get()
        if user is None:
            return [TextContent(type="text", text=json.dumps({
                "error": "Unauthorized: MCP call has no authenticated user"
            }))]

        result = await dispatch_tool(
            name, arguments, user, timeout=MCP_TOOL_TIMEOUT_SECONDS,
            transport="mcp", source_ip=_connection_source_ip.get(),
        )
        return [TextContent(type="text", text=result.content)]
