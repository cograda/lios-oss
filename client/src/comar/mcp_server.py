"""Local MCP server — Claude Code's single point of access.

Merges local tools (vault, reminders via EventKit) with proxied server tools
into one flat tool list on localhost. Runs on localhost:{mcp_port}.

Tool lists are mutable — the daemon can refresh proxied tools after
server reconnection via refresh_server_tools().
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, GetPromptResult, Prompt, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from comar.config import ClientConfig
from comar.server_client import ServerClient
from comar.mcp_https_shim import register_proxied_tools
from comar.prompts import PromptStore

# Type hint only — avoid circular import at runtime
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from comar.eventkit import ReminderStore
    from comar.vault_watcher import VaultHandler

logger = logging.getLogger(__name__)


class CanaryLoggingMiddleware:
    """Logs every request to the localhost MCP app except /health.

    Temporary instrumentation (added 2026-07-07/10) to prove nothing still
    calls this legacy localhost proxy before it's deleted. Tag lines with
    "mcp-canary:" so a later `grep` for zero non-health traffic is trivial.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = scope.get("path", "?")
        client = scope.get("client")
        client_host = client[0] if client else "?"
        logger.info("mcp-canary: %s %s from %s", method, path, client_host)
        await self.app(scope, receive, send)


class ToolStore:
    """Mutable container for tools and handlers.

    MCP handler closures reference this object, so updating its contents
    takes effect immediately without re-registering MCP handlers.

    Tool names use underscores natively (e.g. 'vault_read').
    Handlers are keyed by the tool name.
    """

    def __init__(self):
        self.tools: list[Tool] = []
        self.handlers: dict[str, Any] = {}
        self._local_tool_names: set[str] = set()

    def add_local(self, tools: list[Tool], handlers: dict[str, Any]) -> None:
        """Add local tools (vault, reminders). These are never refreshed."""
        self.tools.extend(tools)
        self.handlers.update(handlers)
        self._local_tool_names.update(handlers.keys())

    def set_proxied(self, tools: list[Tool], handlers: dict[str, Any]) -> None:
        """Replace all proxied tools (called on initial load and reconnect).

        Filters out any proxied tools whose names conflict with local tools,
        so local tools (e.g. reminders_add via EventKit) are never overwritten
        by server-proxied versions of the same tool.
        """
        # Remove old proxied tools
        self.tools = [t for t in self.tools if t.name in self._local_tool_names]
        for name in list(self.handlers):
            if name not in self._local_tool_names:
                del self.handlers[name]
        # Add new proxied tools, excluding any that clash with local names
        filtered_tools = [t for t in tools if t.name not in self._local_tool_names]
        filtered_handlers = {k: v for k, v in handlers.items() if k not in self._local_tool_names}
        self.tools.extend(filtered_tools)
        self.handlers.update(filtered_handlers)

    @property
    def local_count(self) -> int:
        return len(self._local_tool_names)

    @property
    def proxied_count(self) -> int:
        return len(self.tools) - len(self._local_tool_names)


def refresh_server_tools(tool_store: ToolStore, server_client: ServerClient) -> int:
    """Re-fetch server tools and update the tool store.

    Called by the daemon after successful reconnection. Returns the
    number of proxied tools registered.
    """
    try:
        server_tool_defs = server_client.list_tools()
    except Exception:
        logger.exception("Failed to list server tools")
        return 0
    proxy_tools, proxy_handlers = register_proxied_tools(server_tool_defs, server_client)
    tool_store.set_proxied(proxy_tools, proxy_handlers)
    logger.info(f"Tool refresh: {len(proxy_tools)} proxied tools registered")
    return len(proxy_tools)


# Fallback used only if the server is unreachable on first boot.
# Canonical copy lives server-side at app/mcp/instructions.py and is fetched
# via GET /api/v1/instructions — keeps both MCP servers consistent.
_FALLBACK_INSTRUCTIONS = "Comar (Co-Managed Archive) — family knowledge system. Server offline; tools may have limited context."


def create_mcp_app(
    config: ClientConfig, server_client: ServerClient,
    vault_handler: "VaultHandler | None" = None,
    reminder_store: "ReminderStore | None" = None,
    prompt_store: "PromptStore | None" = None,
    supervisor=None,
) -> tuple[Starlette, Server, ToolStore]:
    """Create the MCP server and ASGI app.

    Returns (starlette_app, mcp_server, tool_store) so the daemon can
    manage lifecycle and refresh tools after reconnection.
    """
    # Fetch the canonical instructions block from the server. Falls back to a
    # one-line stub if the server is unreachable so the MCP server still boots.
    try:
        instructions = server_client.get_instructions() or _FALLBACK_INSTRUCTIONS
    except Exception:
        instructions = _FALLBACK_INSTRUCTIONS

    mcp = Server("comar-client", instructions=instructions)
    store = ToolStore()

    # 1. Local vault tools
    vault_tools, vault_handlers = _build_vault_tools(config, vault_handler)
    store.add_local(vault_tools, vault_handlers)

    # 2. Local reminder tools (EventKit — instant reads/writes)
    if reminder_store and reminder_store.is_available:
        rem_tools, rem_handlers = _build_reminder_tools(reminder_store, server_client)
        store.add_local(rem_tools, rem_handlers)

    # 3. Proxied server tools (fetched via HTTP /api/v1/tools)
    try:
        server_tool_defs = server_client.list_tools()
        proxy_tools, proxy_handlers = register_proxied_tools(server_tool_defs, server_client)
        store.set_proxied(proxy_tools, proxy_handlers)
    except Exception:
        logger.exception("Failed to fetch server tools — proxied tools unavailable")

    logger.info(
        f"MCP server: {len(store.tools)} tools "
        f"({store.local_count} local, {store.proxied_count} proxied)"
    )

    # -- Register MCP handlers --

    @mcp.list_tools()
    async def list_tools() -> list[Tool]:
        return store.tools

    @mcp.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        if name not in store.handlers:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )

        handler = store.handlers[name]
        arguments = arguments or {}

        try:
            result = await asyncio.to_thread(handler, arguments)
            content = result if isinstance(result, list) else [TextContent(type="text", text=str(result))]
            return CallToolResult(content=content)
        except Exception as e:
            logger.exception(f"Tool {name} failed")
            return CallToolResult(
                content=[TextContent(type="text", text=str(e))],
                isError=True,
            )

    # -- Register MCP prompt handlers --

    if prompt_store:
        @mcp.list_prompts()
        async def list_prompts() -> list[Prompt]:
            return prompt_store.list_prompts()

        @mcp.get_prompt()
        async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
            return prompt_store.get_prompt(name, arguments or {})

        logger.info(f"MCP prompts: {prompt_store.count} prompts registered")

    # -- Streamable HTTP transport --

    session_manager = StreamableHTTPSessionManager(
        app=mcp,
        json_response=False,
        stateless=False,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    # ASGI handler that delegates to the session manager
    class MCPHandler:
        async def __call__(self, scope, receive, send):
            await session_manager.handle_request(scope, receive, send)
    mcp_handler = MCPHandler()

    async def handle_health(request: Request):
        return JSONResponse({
            "status": "ok",
            "tools": len(store.tools),
            "tools_local": store.local_count,
            "tools_proxied": store.proxied_count,
            "prompts": prompt_store.count if prompt_store else 0,
            "server_connected": server_client.is_connected,
            "vault_watcher_active": vault_handler is not None,
            "retry_queue_depth": vault_handler.retry_queue_depth if vault_handler else 0,
            "tasks": supervisor.health() if supervisor else {},
        })

    import contextlib
    from collections.abc import AsyncIterator

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/health", endpoint=handle_health),
            Route("/mcp", endpoint=mcp_handler),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(CanaryLoggingMiddleware)

    return app, mcp, store


# ---------------------------------------------------------------------------
# Local vault tools
# ---------------------------------------------------------------------------

def _get_mtime_iso(fp: Path) -> str:
    """Get file mtime as ISO timestamp string."""
    from datetime import datetime, timezone
    mtime = fp.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _check_mtime(fp: Path, expected_mtime: str | None) -> str | None:
    """Check if file's mtime matches expected. Returns error message if stale, None if ok."""
    if not expected_mtime or not fp.is_file():
        return None
    from datetime import datetime, timezone
    try:
        expected = datetime.fromisoformat(expected_mtime)
        actual = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)
        # Allow 1-second tolerance (filesystem mtime granularity)
        if actual > expected and (actual - expected).total_seconds() > 1.0:
            return (
                f"File was modified since you read it "
                f"(expected {expected_mtime}, actual {actual.isoformat()}). "
                f"Re-read the file before writing."
            )
    except (ValueError, OSError):
        pass
    return None


def _recently_modified_warning(vault_handler: "VaultHandler | None", rel_path: str) -> str:
    """Return warning string if file was recently modified externally, or empty string."""
    if not vault_handler:
        return ""
    recent = vault_handler.recently_modified()
    if rel_path in recent:
        secs = recent[rel_path]
        return f" ⚠️ This file was modified {secs}s ago (likely open in Obsidian)."
    return ""


def _build_vault_tools(
    config: ClientConfig,
    vault_handler: "VaultHandler | None" = None,
) -> tuple[list[Tool], dict[str, Any]]:
    vault_path = Path(config.vault.path)
    tools = []
    handlers = {}

    # vault_read — read a vault file by path
    tools.append(Tool(
        name="vault_read",
        description="Read a file from the Obsidian vault by relative path. Returns JSON with the file content and its modification time (mtime). Pass the mtime as expected_mtime in subsequent vault_write or vault_edit calls to detect conflicts.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within the vault (e.g. 'Daily Notes/Alex/2026-03-28.md')"},
            },
            "required": ["path"],
        },
    ))

    def vault_read(arguments: dict) -> list[TextContent]:
        rel = arguments.get("path", "")
        fp = vault_path / rel
        if not fp.is_file():
            # Help the model recover: show what IS in the parent folder
            parent = fp.parent
            hint = ""
            if parent.is_dir():
                siblings = sorted(p.name for p in parent.iterdir() if not p.name.startswith("."))
                if siblings:
                    hint = f"\n\nFiles in '{parent.relative_to(vault_path)}/':\n" + "\n".join(f"  {s}" for s in siblings[:20])
            raise FileNotFoundError(f"File not found: {rel}{hint}")
        content = fp.read_text(encoding="utf-8", errors="replace")
        mtime = _get_mtime_iso(fp)
        return [TextContent(type="text", text=json.dumps({"content": content, "mtime": mtime}))]

    handlers["vault_read"] = vault_read

    # vault_list — list files in a vault folder
    tools.append(Tool(
        name="vault_list",
        description="List files and subfolders in a vault folder. Returns name, relative path, and whether each entry is a directory. Use to browse the vault structure.",
        inputSchema={
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Relative folder path (e.g. 'Daily Notes/Alex'). Empty string for vault root.", "default": ""},
            },
            "required": [],
        },
    ))

    def vault_list(arguments: dict) -> list[TextContent]:
        folder = arguments.get("folder", "")
        target = vault_path / folder if folder else vault_path
        if not target.is_dir():
            raise FileNotFoundError(f"Folder not found: {folder}")
        files = []
        for p in sorted(target.iterdir()):
            rel = str(p.relative_to(vault_path))
            if p.name.startswith("."):
                continue
            files.append({"name": p.name, "path": rel, "is_dir": p.is_dir()})
        return [TextContent(type="text", text=json.dumps(files))]

    handlers["vault_list"] = vault_list

    # vault_grep — search vault content
    tools.append(Tool(
        name="vault_grep",
        description="Search vault files for a text pattern (case-insensitive keyword search). Returns matching lines with file paths and line numbers. For meaning-based search, use vault_search on the server instead.",
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text pattern to search for (case-insensitive)"},
                "folder": {"type": "string", "description": "Limit search to a folder (relative path)", "default": ""},
                "limit": {"type": "integer", "description": "Max results to return", "default": 20},
            },
            "required": ["pattern"],
        },
    ))

    def vault_grep(arguments: dict) -> list[TextContent]:
        pattern = arguments.get("pattern", "").lower()
        folder = arguments.get("folder", "")
        limit = arguments.get("limit", 20)
        search_root = vault_path / folder if folder else vault_path

        skip_dirs = {".obsidian", ".claude", ".tools", ".embeddings", ".trash", ".git", "Attachments", "Templates"}
        results = []

        for md_file in sorted(search_root.rglob("*.md")):
            parts = md_file.relative_to(vault_path).parts
            if any(part in skip_dirs for part in parts):
                continue
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(content.splitlines(), 1):
                    if pattern in line.lower():
                        results.append({
                            "file": str(md_file.relative_to(vault_path)),
                            "line": i,
                            "text": line.strip()[:200],
                        })
                        if len(results) >= limit:
                            break
            except Exception:
                continue
            if len(results) >= limit:
                break

        return [TextContent(type="text", text=json.dumps(results))]

    handlers["vault_grep"] = vault_grep

    # vault_write — create or overwrite a vault file
    tools.append(Tool(
        name="vault_write",
        description=(
            "Write content to a vault file. Creates the file (and parent directories) "
            "if it doesn't exist, or overwrites if it does. Use for creating new notes, "
            "daily notes, meeting notes, etc. Pass expected_mtime from a prior vault_read "
            "to detect conflicts (file modified by Obsidian or another process since you read it)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within the vault (e.g. 'Daily Notes/Alex/2026-03-30.md')"},
                "content": {"type": "string", "description": "Full file content to write"},
                "expected_mtime": {"type": "string", "description": "ISO timestamp from a prior vault_read — write is rejected if the file was modified since (optional)"},
            },
            "required": ["path", "content"],
        },
    ))

    def vault_write(arguments: dict) -> list[TextContent]:
        rel = arguments.get("path", "")
        content = arguments.get("content", "")
        expected_mtime = arguments.get("expected_mtime")
        fp = (vault_path / rel).resolve()
        # Security: ensure path stays within vault
        if not str(fp).startswith(str(vault_path.resolve())):
            raise ValueError(f"Path escapes vault: {rel}")
        # Conflict check
        if expected_mtime and fp.is_file():
            err = _check_mtime(fp, expected_mtime)
            if err:
                return [TextContent(type="text", text=json.dumps({"error": err}))]
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        mtime = _get_mtime_iso(fp)
        warning = _recently_modified_warning(vault_handler, rel)
        msg = f"Written {len(content)} chars to {rel}.{warning}"
        return [TextContent(type="text", text=json.dumps({"status": "ok", "message": msg, "mtime": mtime}))]

    handlers["vault_write"] = vault_write

    # vault_edit — targeted edit of a vault file
    tools.append(Tool(
        name="vault_edit",
        description=(
            "Edit a vault file: append to end, append after a heading, or replace a section. "
            "Use 'append' mode to add content at the end. "
            "Use 'after_heading' mode to insert content after a specific heading. "
            "Use 'replace_section' mode to replace all content under a heading (up to the next same-level heading). "
            "Pass expected_mtime from a prior vault_read to detect conflicts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within the vault"},
                "mode": {
                    "type": "string",
                    "enum": ["append", "after_heading", "replace_section"],
                    "description": "Edit mode: append to end, insert after heading, or replace section",
                },
                "content": {"type": "string", "description": "Content to insert or replace with"},
                "heading": {
                    "type": "string",
                    "description": "Target heading text (without # prefix) for after_heading and replace_section modes",
                },
                "expected_mtime": {"type": "string", "description": "ISO timestamp from a prior vault_read — edit is rejected if the file was modified since (optional)"},
            },
            "required": ["path", "mode", "content"],
        },
    ))

    def vault_edit(arguments: dict) -> list[TextContent]:
        rel = arguments.get("path", "")
        mode = arguments.get("mode", "append")
        new_content = arguments.get("content", "")
        heading = arguments.get("heading", "")
        expected_mtime = arguments.get("expected_mtime")

        fp = (vault_path / rel).resolve()
        if not str(fp).startswith(str(vault_path.resolve())):
            raise ValueError(f"Path escapes vault: {rel}")
        if not fp.is_file():
            raise FileNotFoundError(f"File not found: {rel}")
        # Conflict check
        if expected_mtime:
            err = _check_mtime(fp, expected_mtime)
            if err:
                return [TextContent(type="text", text=json.dumps({"error": err}))]

        existing = fp.read_text(encoding="utf-8", errors="replace")

        if mode == "append":
            result = existing.rstrip("\n") + "\n\n" + new_content + "\n"

        elif mode == "after_heading":
            if not heading:
                raise ValueError("heading is required for after_heading mode")
            lines = existing.split("\n")
            insert_idx = None
            for i, line in enumerate(lines):
                stripped = line.lstrip("#").strip()
                if stripped.lower() == heading.lower() and line.strip().startswith("#"):
                    insert_idx = i + 1
                    break
            if insert_idx is None:
                raise ValueError(f"Heading not found: {heading}")
            lines.insert(insert_idx, "\n" + new_content)
            result = "\n".join(lines)

        elif mode == "replace_section":
            if not heading:
                raise ValueError("heading is required for replace_section mode")
            lines = existing.split("\n")
            start_idx = None
            heading_level = 0
            for i, line in enumerate(lines):
                stripped = line.lstrip("#").strip()
                if stripped.lower() == heading.lower() and line.strip().startswith("#"):
                    start_idx = i
                    heading_level = len(line) - len(line.lstrip("#"))
                    break
            if start_idx is None:
                raise ValueError(f"Heading not found: {heading}")
            # Find end of section (next heading at same or higher level)
            end_idx = len(lines)
            for i in range(start_idx + 1, len(lines)):
                line = lines[i]
                if line.strip().startswith("#"):
                    level = len(line) - len(line.lstrip("#"))
                    if level <= heading_level:
                        end_idx = i
                        break
            # Replace section content (keep the heading line)
            new_lines = lines[:start_idx + 1] + ["\n" + new_content + "\n"] + lines[end_idx:]
            result = "\n".join(new_lines)

        else:
            raise ValueError(f"Unknown mode: {mode}")

        fp.write_text(result, encoding="utf-8")
        mtime = _get_mtime_iso(fp)
        warning = _recently_modified_warning(vault_handler, rel)
        msg = f"Edited {rel} ({mode}).{warning}"
        return [TextContent(type="text", text=json.dumps({"status": "ok", "message": msg, "mtime": mtime}))]

    handlers["vault_edit"] = vault_edit

    return tools, handlers


# ---------------------------------------------------------------------------
# Local reminder tools (EventKit)
# ---------------------------------------------------------------------------

def _build_reminder_tools(
    reminder_store: "ReminderStore", server_client: ServerClient,
) -> tuple[list[Tool], dict[str, Any]]:
    """Build local reminder tools that use EventKit for instant reads/writes.

    After each write, triggers a push to the server so it stays in sync.
    """
    tools = []
    handlers = {}

    # reminders_add — create a reminder via EventKit (instant, no command queue)
    tools.append(Tool(
        name="reminders_add",
        description=(
            "Create a new Apple Reminder instantly via EventKit. "
            "Appears on all Apple devices immediately via iCloud."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "The reminder text (required).",
                },
                "list": {
                    "type": "string",
                    "description": "Which reminder list to add to (default: 'Reminders').",
                    "default": "Reminders",
                },
                "notes": {
                    "type": "string",
                    "description": "Additional notes/details (optional).",
                },
                "due_date": {
                    "type": "string",
                    "description": "Due date in ISO format, e.g. '2026-04-01' or '2026-04-01T09:00:00' (optional).",
                },
                "priority": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high"],
                    "description": "Priority level (default: 'none').",
                    "default": "none",
                },
                "account_email": {
                    "type": "string",
                    "description": (
                        "Optional. iCloud account email to write into when this Mac is "
                        "signed into more than one. Only needed if the same list name "
                        "exists in multiple accounts."
                    ),
                },
            },
            "required": ["summary"],
        },
    ))

    def reminders_add(arguments: dict) -> list[TextContent]:
        summary = arguments.get("summary", "").strip()
        if not summary:
            return [TextContent(type="text", text=json.dumps({"error": "summary is required"}))]

        uid = reminder_store.add_reminder(
            title=summary,
            list_name=arguments.get("list", "Reminders"),
            due_date=arguments.get("due_date"),
            priority=arguments.get("priority", "none"),
            notes=arguments.get("notes"),
            account_email=arguments.get("account_email"),
        )

        # Push updated state to server in background
        _push_reminders_to_server(reminder_store, server_client)

        return [TextContent(type="text", text=json.dumps({
            "status": "created",
            "uid": uid,
            "summary": summary,
            "message": f"Reminder '{summary}' created — visible on all Apple devices now.",
        }))]

    handlers["reminders_add"] = reminders_add

    # reminders_complete — mark a reminder done via EventKit (instant)
    tools.append(Tool(
        name="reminders_complete",
        description="Mark a reminder as completed instantly via EventKit. Use the UID from reminders_list. Updates across all Apple devices immediately.",
        inputSchema={
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "The unique ID of the reminder to complete.",
                },
            },
            "required": ["uid"],
        },
    ))

    def reminders_complete(arguments: dict) -> list[TextContent]:
        uid = arguments.get("uid", "").strip()
        if not uid:
            return [TextContent(type="text", text=json.dumps({"error": "uid is required"}))]

        success = reminder_store.complete_reminder(uid)
        if not success:
            return [TextContent(type="text", text=json.dumps({
                "error": f"Reminder {uid} not found or could not be completed",
            }))]

        # Push updated state to server in background
        _push_reminders_to_server(reminder_store, server_client)

        return [TextContent(type="text", text=json.dumps({
            "status": "completed",
            "uid": uid,
        }))]

    handlers["reminders_complete"] = reminders_complete

    # reminders_update — edit fields on an existing reminder via EventKit (instant)
    tools.append(Tool(
        name="reminders_update",
        description=(
            "Update fields on an existing Apple Reminder by its UID. "
            "All fields except uid are optional — only provided fields are changed. "
            "Pass due_date='' to clear the due date."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "The unique ID of the reminder to update.",
                },
                "summary": {
                    "type": "string",
                    "description": "New reminder text (optional).",
                },
                "notes": {
                    "type": "string",
                    "description": "New notes/details (optional).",
                },
                "due_date": {
                    "type": "string",
                    "description": "New due date in ISO format, e.g. '2026-04-01T09:00:00'. Pass '' to clear.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high"],
                    "description": "New priority level (optional).",
                },
            },
            "required": ["uid"],
        },
    ))

    def reminders_update(arguments: dict) -> list[TextContent]:
        uid = arguments.get("uid", "").strip()
        if not uid:
            return [TextContent(type="text", text=json.dumps({"error": "uid is required"}))]

        kwargs = {k: arguments[k] for k in ("summary", "notes", "due_date", "priority") if k in arguments}
        if not kwargs:
            return [TextContent(type="text", text=json.dumps({"error": "at least one field to update is required"}))]

        success = reminder_store.update_reminder(uid=uid, **kwargs)
        if not success:
            return [TextContent(type="text", text=json.dumps({
                "error": f"Reminder {uid} not found or could not be updated",
            }))]

        _push_reminders_to_server(reminder_store, server_client)

        return [TextContent(type="text", text=json.dumps({
            "status": "updated",
            "uid": uid,
            "fields": list(kwargs.keys()),
        }))]

    handlers["reminders_update"] = reminders_update

    return tools, handlers


def _push_reminders_to_server(
    reminder_store: "ReminderStore", server_client: ServerClient,
) -> None:
    """Read all reminders via EventKit and push to the server.

    Best-effort — logs errors but doesn't raise.
    """
    try:
        reminders = reminder_store.read_all_reminders()
        server_client.push_reminders(reminders)
    except Exception:
        logger.exception("Failed to push reminders to server after local change")
