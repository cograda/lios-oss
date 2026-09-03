"""Unified tool dispatcher — V4 chunk 2.1.

The single chokepoint both transports (`app/mcp/server.py::call_tool` and
`app/api/v1.py::call_tool`) route every tool invocation through. Before this
chunk each transport re-implemented the same pipeline independently; now
both are thin adapters that resolve their own (transport-specific) auth,
then call `dispatch_tool()` with an already-authenticated `User`.

Pipeline, in order: handler lookup -> user-scoping (`use_user`) -> freshness
check (`ensure_fresh`) -> timeout-bounded handler invocation -> `tool_calls`
persistence -> structured completion log line -> error normalization.

Auth is deliberately NOT handled here — `dispatch_tool` takes a `User` as an
explicit argument and never reads any ambient/ContextVar auth state itself.
This is also the intended hook point for capability enforcement (chunk 2.2)
and audit (chunk 2.5): both need exactly one chokepoint to attach to, and
this module is it.

V4 chunk 2.5: every call's arguments are redacted (`app.services.redaction
.scrub_args`) and persisted as `tool_calls.args_summary`, alongside the
caller's `transport` and `source_ip` (both supplied by the transport adapter
— this module never guesses them). Write handlers may optionally call
`set_affected([...])` during their own execution to record which entities
they touched (e.g. `["snag:SNAG-0042"]`); most handlers don't, and `affected`
stays null.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from app.auth.context import use_user
from app.db import get_db
from app.errors import PermanentError
from app.models.users import User
from app.plugin.registry import get_tool_handler, tool_metadata
from app.services.redaction import scrub_args
from app.services.tool_calls import record_tool_call
from app.services.vault_grants import resolve_effective_user_id

logger = logging.getLogger(__name__)

# Ceiling for any single tool call, shared by both transports. MCP tools
# should be fast queries; long-running work (backfills, embeds) goes via
# /api/* endpoints instead.
#
# Before this chunk: `app/mcp/server.py` had a named constant
# `MCP_TOOL_TIMEOUT_SECONDS = 60`; `app/api/v1.py` had a bare literal `60`
# inline in the `asyncio.wait_for(...)` call. Same value, so no behavior
# change — but `MCP_TOOL_TIMEOUT_SECONDS` stays a module attribute on
# `app.mcp.server` (now just set to this constant) because
# `tests/test_mcp_transport.py::test_tool_timeout_returns_error_payload`
# monkeypatches it directly to force a fast timeout; the MCP adapter passes
# its own module's value through explicitly so that test keeps working
# unchanged.
TOOL_TIMEOUT_SECONDS = 60

# Per-call correlation id. Bound around the dispatch of one tool so any
# logging done inside a handler during that window can correlate back to
# the structured completion line and the persisted `tool_calls` row via
# `current_tool_call_id()`.
_tool_call_id: ContextVar[str | None] = ContextVar("tool_call_id", default=None)


def current_tool_call_id() -> str | None:
    """Return the id of the in-flight tool call, or None outside one."""
    return _tool_call_id.get()


@contextmanager
def bind_tool_call_id(call_id: str) -> Iterator[None]:
    """Bind `call_id` on `_tool_call_id` for the duration of the with-block."""
    token = _tool_call_id.set(call_id)
    try:
        yield
    finally:
        _tool_call_id.reset(token)


# Per-call "what did this write touch" list, set (optionally) by a write
# handler during its own execution and read back by `dispatch_tool` once the
# handler returns — see `set_affected()`. Handler code runs inside
# `asyncio.to_thread`, i.e. a copied contextvars.Context; a `set()` made
# there is only visible within that same copied context, which is exactly
# what we want: `_run()` below reads it back in the same thread, right after
# calling the handler, and returns it alongside the handler's result.
_affected_refs: ContextVar[list[str] | None] = ContextVar(
    "tool_affected_refs", default=None,
)


def set_affected(refs: list[str]) -> None:
    """Record which entities a write touched, for the `tool_calls` audit row.

    Optional — call this from inside a write-tool handler (session, arguments)
    after a successful write, e.g. `set_affected([f"snag:{snag.uid}"])`. Tools
    that don't call this leave `affected` null on their `tool_calls` row.
    First adopters: snag_add/snag_update, reminders_add/reminders_complete.
    """
    _affected_refs.set(list(refs))


@dataclass
class ToolResult:
    """Transport-agnostic outcome of one dispatched tool call.

    `content` is always a string: the handler's raw return value (JSON-
    encoded if the handler didn't already return a string) on success, or a
    `json.dumps({"error": ...})` string on failure — matching what both
    transports returned before this chunk.
    """

    content: str
    is_error: bool
    tool_call_id: str | None
    status: str = "ok"  # "ok" | "error" | "timeout" — lets HTTP pick a status code


async def dispatch_tool(
    name: str,
    arguments: dict[str, Any] | None,
    user: User,
    *,
    timeout: float = TOOL_TIMEOUT_SECONDS,
    transport: str | None = None,
    source_ip: str | None = None,
) -> ToolResult:
    """Dispatch one tool call for an already-authenticated `user`.

    Auth is the caller's job — this never reads ambient auth state. Unknown
    tools are reported without touching the `tool_calls` table or emitting
    the completion log line (matches the pre-chunk behavior of both
    transports, which both returned early on an unknown name before any of
    that bookkeeping ran).

    `transport` (`"mcp"` | `"http"`) and `source_ip` are supplied by the
    transport adapter — this module never guesses them — and land verbatim
    on the persisted `tool_calls` row (V4 chunk 2.5).
    """
    handler_entry = get_tool_handler(name)
    if handler_entry is None:
        return ToolResult(
            content=json.dumps({"error": f"Unknown tool: {name}"}),
            is_error=True,
            tool_call_id=None,
            status="error",
        )
    handler_fn, integration_name = handler_entry
    arguments = arguments or {}
    # Read off the ORM instance once, here. `user` was loaded by the transport's
    # own session and is detached by the time the worker thread runs; touching a
    # lazy attribute there would raise inside the access check.
    user_id = user.id
    user_name = user.name

    # --- capability check (chunk 2.2) ---------------------------------------
    # First occupant of the hook this module's docstring reserved: a read-only
    # cross-user grant. Resolution needs a session, so it happens inside
    # `_run()` below, immediately before the `use_user(...)` binding it decides
    # — the two must not be separable, or a later edit could bind one user
    # having authorised another.

    from app.services.freshness import ensure_fresh

    call_id = secrets.token_hex(4)
    start = time.monotonic()
    status = "ok"
    error_text: str | None = None
    result: str | None = None
    affected: list[str] | None = None
    args_summary = scrub_args(arguments)

    with bind_tool_call_id(call_id):
        try:
            def _run():
                db = get_db()
                with db.session() as session:
                    effective_user_id = resolve_effective_user_id(
                        session,
                        caller_id=user_id,
                        caller_name=user_name,
                        arguments=arguments,
                        tool_name=name,
                        integration_name=integration_name,
                        annotations=(tool_metadata.get(name) or {}).get("annotations"),
                    )
                    with use_user(effective_user_id):
                        ensure_fresh(integration_name, session)
                        _affected_refs.set(None)
                        handler_result = handler_fn(session, arguments)
                        return handler_result, _affected_refs.get()

            result, affected = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
            if not isinstance(result, str):
                result = json.dumps(result)
        except asyncio.TimeoutError:
            status = "timeout"
            error_text = f"Tool {name} timed out after {timeout}s"
            logger.warning(
                "Tool %s timed out after %ss (user=%s)", name, timeout, user_id,
            )
        except PermanentError as e:
            # Bad credentials / config, not a bug worth a stack trace every
            # call — warn (no traceback spam) and return a structured error.
            status = "error"
            error_text = str(e)
            logger.warning("Tool %s failed (permanent, user=%s): %s", name, user_id, e)
        except Exception as e:  # noqa: BLE001
            status = "error"
            error_text = str(e)
            logger.exception("Tool %s failed (user=%s)", name, user_id)
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
                args_summary=args_summary, affected=affected,
                source_ip=source_ip, transport=transport,
            )

    if status == "ok":
        return ToolResult(content=result, is_error=False, tool_call_id=call_id, status="ok")
    return ToolResult(
        content=json.dumps({"error": error_text}),
        is_error=True,
        tool_call_id=call_id,
        status=status,
    )
