"""V3 client HTTP API — `/api/v1/*`.

Endpoints:
  GET  /api/v1/tools                  — list registered MCP tools
  POST /api/v1/tools/{name}           — invoke a registered MCP tool
  POST /api/v1/batch                  — invoke several tools in one request
  POST /api/v1/vault/push             — push a single vault file (write + index)
  POST /api/v1/reminders/push         — push reminders snapshot
  GET  /api/v1/events                 — Server-Sent Events stream (server→client)

All endpoints require an `Authorization: Bearer <client_token>` header
validated against the `client_tokens` table. The authenticated user is
exposed to handlers via `Depends(get_current_user)`.

## The envelope, and why `/tools/{name}` has two shapes

Per the "one data layer" plan (`vault/Projects/lios/Plans/2026-09-11 One
data layer…md`, §4.2/§5 step 1): every projection response is meant to
converge on one envelope — `{"ok": true, "result": …, "warnings": [...]?}`
or `{"ok": false, "error": {"code", "message", "retryable"}}`.

`POST /api/v1/batch` speaks it unconditionally — it's new, so there's no
deployed contract to protect. `POST /api/v1/tools/{name}` is not new: the
lios-sync daemon (`core/client/src/lios_sync/server_client.py::call_tool`)
and `apps/loops` (`apps/loops/backend/app/core_api.py::call_tool`) are both
deployed today reading its *current* shape — `{"ok": false, "error": "<str>"}`
on failure, no `warnings` key — and neither is part of this change. Breaking
either is worse than a second code path, so the richer shape is opt-in on
this route: send `X-Lios-Envelope: 1` (or `Accept:
application/vnd.lios.envelope+json`) to get `error` as an object, and a
`warnings` array whenever a tool result carries one.

⚠️ **The `warnings` array's only populator was the tasks tools'
`render_skipped`/`render_error` pattern (#204), and that pattern is gone**
(lios, 2026-09-14 — `Task Backlog.md`'s render became unconditional, so it
never reports a skipped render any more; see `app/integrations/tasks/render.py`'s
module docstring). `_warnings_from_result`-the-folder was removed with it.
`warnings` stays part of the envelope shape for any future tool that wants
to report a partial-success condition the same way — there is simply no
current producer.
"""

import asyncio
import json
import logging
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.api.error_codes import error_obj
from app.api.freshness_hints import freshness_seconds
from app.auth.client_token import get_current_user
from app.config import settings
from app.db import get_db
from app.models.users import User
from app.stream_manager import DASHBOARD_CHANNEL_USER, stream_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["v1"])

# ---------------------------------------------------------------------------
# Envelope negotiation + shared helpers (batch, and opt-in on /tools/{name})
# ---------------------------------------------------------------------------

_ENVELOPE_HEADER = "x-lios-envelope"
_ENVELOPE_ACCEPT_SUFFIX = "application/vnd.lios.envelope+json"


def _envelope_requested(request: Request) -> bool:
    """Whether the caller opted into the richer envelope on `/tools/{name}`.

    Batch always speaks the envelope; this is only consulted by the single-
    tool route. See the module docstring for why it's opt-in there.
    """
    if request.headers.get(_ENVELOPE_HEADER) == "1":
        return True
    return _ENVELOPE_ACCEPT_SUFFIX in request.headers.get("accept", "")


def _parse_tool_result(content: str) -> Any:
    """Tool handlers conventionally return a JSON-encoded string. Parse it
    so the HTTP response carries structured data, not a string-of-JSON."""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content  # leave as raw string if not JSON


def _classify_dispatch_error(outcome: "ToolResult") -> tuple[str, str]:
    """Best-effort (code, message) from a dispatch `ToolResult`'s failure.

    `dispatch_tool()` collapses every handler failure — bad args, a
    `PermanentError`, a bare exception, a timeout — into one string (see
    `app/plugin/dispatch.py::ToolResult`). This is therefore pattern
    matching on the handful of messages that already have real structure
    (an unknown-tool 404, a timeout, the read-only-scope refusal); anything
    else is `internal`, which is the honest answer for a message this layer
    can't further classify without dispatch itself carrying a typed error
    (a bigger change than this step).
    """
    try:
        message = json.loads(outcome.content).get("error", outcome.content)
    except (json.JSONDecodeError, TypeError, AttributeError):
        message = str(outcome.content)
    if not isinstance(message, str):
        message = str(message)
    if outcome.status == "timeout":
        return "timeout", message
    if message.startswith("Unknown tool:"):
        return "unknown_tool", message
    if "read-only token" in message or "not read-only" in message:
        return "forbidden", message
    return "internal", message


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

@router.get("/heartbeat")
def heartbeat(
    client_version: str = "",
    task_health: str = "",
    user: User = Depends(get_current_user),
) -> dict:
    """Lightweight liveness probe.

    Returns the latest client wheel version + checksum so the client can
    detect updates with a single poll.
    """
    from datetime import datetime, timezone
    from app.auth.client_token import client_token_id_of
    from app.models.clients import ClientToken

    # F11a: write onto the token that authenticated THIS request, not the
    # user's most-recently-seen active token — the old query raced with the
    # user's other tokens (e.g. a phone's Health Auto Export push bumping
    # last_seen_at between this request and the write below), so a daemon's
    # heartbeat could silently land on a different device's row.
    #
    # OAuth-authenticated sessions have no `client_tokens` row at all —
    # `client_token_id_of` returns None there, and heartbeat is daemon-only
    # in practice, so we just skip the write rather than 500 or guess.
    token_id = client_token_id_of(user)
    if token_id is not None and (client_version or task_health):
        db = get_db()
        with db.session() as session:
            row = session.query(ClientToken).filter_by(id=token_id).first()
            if row:
                if client_version:
                    row.client_version = client_version
                if task_health:
                    row.task_health = task_health[:4000]
                session.commit()
        # Best-effort push so the dashboard's daemon-status card updates on
        # the next heartbeat rather than waiting out its own poll interval
        # (issue #141). This is a "something changed, go refetch" nudge —
        # `daemon_status`'s actual shape still comes from `/api/system/alerts`.
        _notify_dashboard_from_thread("daemon_heartbeat", user=user.name)

    latest_version, latest_checksum = _latest_client_info()
    if client_is_legacy(client_version):
        # Freeze the old channel. A 2.x `comar` daemon told about a `lios_sync`
        # wheel would pipx-install it BESIDE itself, restart its own launchd
        # agent, still report 2.6.3, and repeat every five minutes. Reporting no
        # newer version keeps it quietly on what it has until the machine is
        # re-installed under the new name (Sam's Mac, 2026-09-03).
        latest_version, latest_checksum = "", ""

    return {
        "ok": True,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "latest_client_version": latest_version,
        "latest_client_checksum": latest_checksum,
    }


@router.post("/reminders/verified")
def reminders_verified(user: User = Depends(get_current_user)) -> dict:
    """Stamp the calling user's `reminders_verified_at = now()`.

    Called by the comar-client daemon on every reminders-poll iteration,
    whether or not data changed. This is bridge-liveness — distinct from
    `Reminder.synced_at`, which only moves when EventKit data actually
    changes. data_freshness for apple_reminders reads this field, so a
    silent daemon is detectable even during quiet periods.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    db = get_db()
    with db.session() as session:
        row = session.query(User).filter_by(id=user.id).first()
        if row:
            row.reminders_verified_at = now
            session.commit()
    return {"ok": True, "verified_at": now.isoformat()}


def client_is_legacy(client_version: str) -> bool:
    """True for a pre-rename `comar` daemon (major version < 3). Unknown or
    unparsable versions are NOT legacy: a brand-new client that fails to
    report should still be offered the update."""
    if not client_version:
        return False
    try:
        return int(str(client_version).split(".", 1)[0]) < 3
    except ValueError:
        return False


def _latest_client_info() -> tuple[str, str]:
    """Read latest client wheel version + SHA256. Returns ('', '') if none.

    Shares its wheel-picking logic (version-aware, not a lexical filename
    sort) with `routes/client_dist.py`, which serves the same directory
    over HTTP for daemon auto-update.
    """
    from app.routes.client_dist import _DIST_DIRS, VERSION_RE, _compute_sha256, pick_latest_wheel

    for base in _DIST_DIRS:
        if not base.is_dir():
            continue
        wheel_path = pick_latest_wheel(base.glob("lios_sync-*.whl"))
        if wheel_path is None:
            continue
        m = VERSION_RE.search(wheel_path.name)
        if not m:
            continue
        return m.group(1), _compute_sha256(wheel_path)
    return "", ""


# ---------------------------------------------------------------------------
# Per-user curated slash-command set (sam-rollout Phase B2)
# ---------------------------------------------------------------------------

@router.get("/commands")
def get_commands(user: User = Depends(get_current_user)) -> dict:
    """Return the caller's curated `.claude/commands/*.md` set + CLAUDE.md.

    Rendered per user from `app.prompts.commands`, whose `COMMAND_TABLE`
    says which user gets which command — the single source of truth for
    this command set. Used by the installer to populate a new machine's
    `~/lios/.claude/commands/` and by the daemon's optional startup
    refresh. Alex's copies are delivered to `vault/.claude/commands/` from
    the same templates by `scripts/render_commands.py` rather than by this
    endpoint, because his machine has the repo checked out.
    """
    from app.prompts.commands import (
        RETIRED_COMMANDS,
        render_claude_md,
        render_command_set,
    )
    from app.services import preferences as prefs_service

    # Rendered against the caller's preferences, so a section they've switched
    # off is absent from the delivered file rather than merely suppressed at
    # runtime — see `render_command`'s docstring for why that distinction
    # matters for how long their morning takes.
    db = get_db()
    with db.session() as session:
        prefs = prefs_service.get_all(session, user.id)

    return {
        "user": user.name,
        "claude_md": render_claude_md(user.name, user.display_name),
        "commands": render_command_set(user.name, user.display_name, prefs),
        # Filenames a client may have written under a now-retired slug
        # (`daily-note.md`, `triage.md` as of 2026-09-07) — see
        # `app.prompts.commands`'s docstring. The client deletes any of
        # these it finds and never anything else.
        "retired": list(RETIRED_COMMANDS),
    }


# ---------------------------------------------------------------------------
# MCP server instructions (preamble for the model)
# ---------------------------------------------------------------------------

@router.get("/instructions")
def get_instructions(user: User = Depends(get_current_user)) -> dict:
    """Return the MCP instructions block, personalized for the caller.

    The household-shared core is a single source of truth
    (`app.mcp.instructions.COMAR_INSTRUCTIONS`) mirrored here and at the MCP
    handshake itself. This endpoint additionally knows who's asking (the
    bearer already resolved a `User`), so it appends that user's own
    section — display name, vault paths, only the private integrations they
    actually have data for, and their voice-profile guidance (sam-rollout
    D1 + D2) — via `render_instructions_for_user`. The bare MCP-handshake
    `instructions` field stays the static shared core; see that module's
    docstring for why.
    """
    from app.mcp.instructions import render_instructions_for_user

    db = get_db()
    with db.session() as session:
        rendered = render_instructions_for_user(session, user)
    return {"instructions": rendered}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@router.get("/tools")
def list_tools(_user: User = Depends(get_current_user)) -> list[dict]:
    """Return every registered MCP tool with its JSON schema + metadata."""
    from app.mcp.server import _tool_definitions, _tool_metadata

    tools: list[dict] = []
    for tool_def in _tool_definitions:
        schema = tool_def.inputSchema if hasattr(tool_def, "inputSchema") else {}
        if isinstance(schema, dict) and "required" not in schema:
            schema = {**schema, "required": []}
        meta = _tool_metadata.get(tool_def.name) or {}
        tools.append({
            "name": tool_def.name,
            "description": tool_def.description or "",
            "inputSchema": schema,
            **meta,
        })
    return tools


@router.post("/tools/{name}")
async def call_tool(
    name: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> Response:
    """Invoke a tool by name. Body is the raw JSON arguments object.

    Default response shape mirrors the gRPC `CallToolResponse`: `{"ok":
    true, "result": ...}` or `{"ok": false, "error": "<str>"}` — unchanged
    from before this route grew an envelope, and this is the shape every
    deployed consumer (lios-sync, apps/loops) still gets. Send
    `X-Lios-Envelope: 1` to receive the richer envelope (`error` as an
    object, `warnings` array) instead — see the module docstring.

    Thin adapter over the shared `dispatch_tool()` chokepoint (V4 chunk
    2.1) — auth is resolved above via `Depends(get_current_user)`
    (transport-specific), then handed to `dispatch_tool` as an
    already-authenticated `User`.

    Read-only tools (per `readOnlyHint` — see `app/plugin/dispatch.py`'s use
    of the same annotation) additionally carry `ETag` + `Cache-Control` on
    success, and honour `If-None-Match` with a 304. Freshness (the
    `max-age`) comes from `app.api.freshness_hints`; 0 means `no-store`.
    """
    from app.plugin.dispatch import dispatch_tool
    from app.plugin.registry import get_tool_annotations, get_tool_handler

    envelope = _envelope_requested(request)

    if get_tool_handler(name) is None:
        if envelope:
            return JSONResponse(
                {"ok": False, "error": error_obj("unknown_tool", f"Unknown tool: {name}")},
                status_code=404,
            )
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")

    try:
        body = await request.body()
        arguments: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError as e:
        if envelope:
            return JSONResponse(
                {"ok": False, "error": error_obj("invalid_args", f"Invalid JSON body: {e}")},
                status_code=400,
            )
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

    outcome = await dispatch_tool(
        name, arguments, user,
        transport="http",
        source_ip=request.client.host if request.client else None,
    )

    if outcome.status == "timeout" or outcome.is_error:
        status_code = 504 if outcome.status == "timeout" else 500
        if envelope:
            code, message = _classify_dispatch_error(outcome)
            return JSONResponse({"ok": False, "error": error_obj(code, message)}, status_code=status_code)
        return JSONResponse({"ok": False, "error": json.loads(outcome.content)["error"]}, status_code=status_code)

    parsed = _parse_tool_result(outcome.content)

    resp_body: dict[str, Any] = {"ok": True, "result": parsed}

    annotations = get_tool_annotations(name) or {}
    if not annotations.get("readOnlyHint"):
        return JSONResponse(resp_body)

    # Read-only: cacheable. ETag is a hash of the body that would be sent,
    # so it changes exactly when the response would.
    body_bytes = json.dumps(resp_body, sort_keys=True, default=str).encode("utf-8")
    etag = '"' + sha256(body_bytes).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    max_age = freshness_seconds(name)
    cache_control = f"private, max-age={max_age}" if max_age > 0 else "no-store"
    return JSONResponse(resp_body, headers={"ETag": etag, "Cache-Control": cache_control})


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

class BatchItem(BaseModel):
    id: str | None = None
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class BatchRequest(BaseModel):
    items: list[BatchItem]


async def _dispatch_batch_item(
    item_id: str, tool_name: str, args: dict[str, Any], user: User, source_ip: str | None,
) -> dict:
    """Dispatch one batch item and return its envelope entry.

    Never raises — every failure mode (unknown tool, dispatch error,
    timeout) is folded into `{"id", "ok": False, "error": {...}}` so one
    bad item can never take down the rest of the batch. Reuses
    `dispatch_tool()` — the exact same chokepoint the single-tool route
    calls, with the exact same `user`, so a batch item is scoped identically
    to that same call made on its own: it cannot see or touch more than the
    caller already could.
    """
    from app.plugin.dispatch import dispatch_tool
    from app.plugin.registry import get_tool_handler

    if get_tool_handler(tool_name) is None:
        return {"id": item_id, "ok": False, "error": error_obj("unknown_tool", f"Unknown tool: {tool_name}")}

    outcome = await dispatch_tool(tool_name, args, user, transport="http", source_ip=source_ip)

    if outcome.status == "timeout" or outcome.is_error:
        code, message = _classify_dispatch_error(outcome)
        return {"id": item_id, "ok": False, "error": error_obj(code, message)}

    parsed = _parse_tool_result(outcome.content)
    entry: dict[str, Any] = {"id": item_id, "ok": True, "result": parsed}
    return entry


@router.post("/batch")
async def batch(request: Request, user: User = Depends(get_current_user)) -> JSONResponse:
    """Run several tool calls in one request, concurrently, under one caller.

    Body is `{"items": [{"id", "tool", "args"}, ...]}` — a bare JSON list is
    also accepted as shorthand for `items`. Every item is dispatched through
    the same `dispatch_tool()` chokepoint the single-tool route uses (no
    forked auth/scoping logic), under the same already-authenticated `user`
    — a batch item is exactly as scoped as the same call made on its own,
    so it cannot widen what the caller's own bearer already permits.

    Response is always the envelope: `{"ok": true, "results": [...]}` in
    input order, one entry per item — `{"id", "ok": true, "result": ...,
    "warnings": [...]?}` or `{"id", "ok": false, "error": {"code",
    "message", "retryable"}}`. A per-item failure — an unknown tool, a
    capability/auth refusal, a handler error, a per-item timeout — never
    fails the batch as a whole; only a malformed request (bad JSON, no
    `items`, too many items) returns a top-level `{"ok": false, "error":
    ...}` with a 4xx status.

    `settings.batch_max_items` (default 25) caps items per request.
    `settings.batch_timeout_seconds` (default 30) is the wall-clock budget
    for the *whole* batch — an item still running when it expires is
    reported as its own per-item `timeout`, not a failure of the batch.
    """
    try:
        raw_body = await request.body()
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError as e:
        return JSONResponse(
            {"ok": False, "error": error_obj("invalid_args", f"Invalid JSON body: {e}")},
            status_code=400,
        )

    items_payload = payload if isinstance(payload, list) else (
        payload.get("items") if isinstance(payload, dict) else None
    )
    if not isinstance(items_payload, list):
        return JSONResponse(
            {"ok": False, "error": error_obj(
                "invalid_args", 'body must be a JSON list, or an object with an "items" list',
            )},
            status_code=400,
        )
    if not items_payload:
        return JSONResponse({"ok": True, "results": []})

    max_items = settings.batch_max_items
    if len(items_payload) > max_items:
        return JSONResponse(
            {"ok": False, "error": error_obj(
                "invalid_args",
                f"batch has {len(items_payload)} items, max is {max_items}",
            )},
            status_code=400,
        )

    parsed_items: list[BatchItem] = []
    for idx, raw in enumerate(items_payload):
        try:
            item = raw if isinstance(raw, BatchItem) else BatchItem.model_validate(raw)
        except Exception as e:  # noqa: BLE001 — pydantic ValidationError, any shape
            return JSONResponse(
                {"ok": False, "error": error_obj("invalid_args", f"item {idx}: {e}")},
                status_code=400,
            )
        if not item.id:
            item = item.model_copy(update={"id": f"item-{idx}"})
        parsed_items.append(item)

    source_ip = request.client.host if request.client else None
    tasks = {
        asyncio.ensure_future(
            _dispatch_batch_item(item.id, item.tool, item.args, user, source_ip),
        ): item.id
        for item in parsed_items
    }

    budget = settings.batch_timeout_seconds
    done, pending = await asyncio.wait(tasks.keys(), timeout=budget)

    results_by_id: dict[str, dict] = {}
    for task in done:
        entry = task.result()
        results_by_id[entry["id"]] = entry
    if pending:
        for task in pending:
            task.cancel()
        # Let cancellation actually land before returning, so a pending
        # dispatch doesn't keep running (and keep touching the DB session
        # it opened) after this request has already answered.
        await asyncio.gather(*pending, return_exceptions=True)
        for task in pending:
            item_id = tasks[task]
            results_by_id[item_id] = {
                "id": item_id,
                "ok": False,
                "error": error_obj("timeout", f"batch timeout budget ({budget}s) exceeded"),
            }

    ordered_results = [results_by_id[item.id] for item in parsed_items]
    return JSONResponse({"ok": True, "results": ordered_results})


# ---------------------------------------------------------------------------
# Vault push
# ---------------------------------------------------------------------------

class VaultPushRequest(BaseModel):
    path: str = Field(..., description="Vault-relative path, e.g. 'Daily Notes/Alex/2026-04-28.md'")
    content: str = Field(..., description="Full file content (UTF-8 text)")
    file_hash: str = Field("", description="MD5 of content (advisory; server re-hashes on index)")


@router.post("/vault/push")
def vault_push(
    payload: VaultPushRequest,
    user: User = Depends(get_current_user),
) -> dict:
    """Write a single vault file to the caller's own vault and re-index it."""
    from pathlib import Path
    from app.integrations.obsidian.sync import index_single_file
    from app.services import vault_paths

    # The target is the *caller's* vault, not the legacy single-vault mount —
    # otherwise Sam's daemon would write files into Alex's vault.
    vault_root = vault_paths.user_vault_path(user.name).resolve()
    if not vault_root.is_dir():
        raise HTTPException(status_code=503, detail="Vault not provisioned for user")

    # Containment check — payload.path is bearer-authenticated input but a
    # value like "../../../app/main.py" would otherwise let any client overwrite
    # arbitrary container files (RCE-equivalent on the next reload).
    rel = payload.path.lstrip("/")
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise HTTPException(status_code=400, detail="path escapes vault")
    full = (vault_root / rel).resolve()
    if not str(full).startswith(str(vault_root) + "/") and full != vault_root:
        raise HTTPException(status_code=400, detail="path escapes vault")

    full.parent.mkdir(parents=True, exist_ok=True)

    # Only write when the bytes actually differ. An unconditional write here
    # was an infinite loop, because two transports own this tree at once:
    # Syncthing replicates the vault laptop<->server, and this endpoint writes
    # to the same files. Rewriting identical content still bumps mtime, so —
    #   daemon pushes X -> we rewrite X -> Syncthing carries the new mtime to
    #   the Mac -> fsevents fires -> daemon pushes X -> ...
    # — ran forever at roughly one lap per 8-10s over every file that had ever
    # been pushed (17 of them when this was found on 2026-08-14, live since at
    # least 09 Aug). It cost no embeddings, since index_single_file dedups on
    # file_hash, but it manufactured Syncthing conflict files (two writers, one
    # file) and left mtime meaningless, which is what `vault_recent` reads.
    #
    # The client now also skips no-op pushes; this guard is the backstop, and
    # the one that holds for any other client. Compare content, not hashes: we
    # are already holding both strings, and it cannot disagree with itself.
    try:
        unchanged = full.read_text(encoding="utf-8") == payload.content
    except (OSError, UnicodeDecodeError):
        unchanged = False  # unreadable or not yet there — write it
    if not unchanged:
        full.write_text(payload.content, encoding="utf-8")

    db = get_db()
    with db.session() as session:
        index_single_file(
            session, payload.path, payload.content, payload.file_hash, user.id,
        )

    logger.info("vault/push from %s: %s (%d chars)", user.name, payload.path, len(payload.content))
    return {"ok": True, "path": payload.path, "indexed": True}


# ---------------------------------------------------------------------------
# Reminders push
# ---------------------------------------------------------------------------

class ReminderItem(BaseModel):
    uid: str
    list_name: str
    summary: str
    priority: int = 0
    completed: bool = False
    due_date: str | None = None
    account_email: str | None = Field(
        default=None,
        description="iCloud account that owns the source list. Used in V3 multi-user "
                    "to attribute the reminder to a user. Optional in this phase.",
    )


class RemindersPushRequest(BaseModel):
    reminders: list[ReminderItem]


@router.post("/reminders/push")
def reminders_push(
    payload: RemindersPushRequest,
    user: User = Depends(get_current_user),
) -> dict:
    """Push the full reminders snapshot from a client."""
    from app.integrations.apple_reminders.sync import sync_from_push

    reminders_data: list[dict] = []
    for r in payload.reminders:
        item = {
            "uid": r.uid,
            "list_name": r.list_name,
            "summary": r.summary,
            "priority": r.priority,
            "completed": r.completed,
        }
        if r.due_date:
            item["due_date"] = r.due_date
        if r.account_email:
            # Forward but server-side attribution lands in Phase 2.
            item["account_email"] = r.account_email
        reminders_data.append(item)

    db = get_db()
    with db.session() as session:
        result = sync_from_push(reminders_data, session, user_id=user.id)

    changes = result["newly_completed"] + result["newly_added"] + result["edited"]
    if changes > 0:
        # Reactive reminders-inlet tick, so a reminder completed on the phone
        # reaches the ledger within seconds rather than waiting for the next
        # 15-minute `reminders_inlet_tick`. Threads do not propagate
        # ContextVars, so we must rebind inside the worker.
        _trigger_reminders_inlet(user.id)

    logger.info(
        "reminders/push from %s: %d items (%d changes)",
        user.name, len(payload.reminders), changes,
    )
    return {
        "ok": True,
        "count": result["count"],
        "newly_completed": result["newly_completed"],
        "newly_added": result["newly_added"],
        "edited": result["edited"],
    }


class CommandDonePayload(BaseModel):
    result: dict | None = None
    error: str | None = None


@router.post("/reminders/commands/{command_id}/done")
def reminders_command_done(
    command_id: int,
    payload: CommandDonePayload,
    user: User = Depends(get_current_user),
) -> dict:
    """Daemon ack for an SSE-dispatched EventKit command.

    Resolves the waiting Future on the server so dispatch_command can return
    synchronously to the MCP caller. Caller (the daemon) authenticates with
    its own bearer; we verify ownership before resolving.
    """
    from app.integrations.apple_reminders.commands import complete_command

    db = get_db()
    with db.session() as session:
        ok = complete_command(
            session,
            command_id=command_id,
            user_id=user.id,
            result=payload.result,
            error=payload.error,
        )
    if not ok:
        raise HTTPException(status_code=404, detail=f"Command {command_id} not found")
    return {"ok": True, "command_id": command_id}


def _trigger_reminders_inlet(user_id: int) -> None:
    """Fire-and-forget reminders-inlet tick after a push that changed something.

    `user_id` is the pushing user's id, snapshotted before spawning the
    thread — threading.Thread does not propagate ContextVars, so anything
    the tick needs bound has to be rebound inside the worker. `tick_once`
    itself iterates every active user and binds `use_user` per-user
    internally (same as its own cron entry, `reminders_inlet.run_tick`,
    which calls it unwrapped) — the rebind here exists only in case a
    future rule needs the *pushing* user as ambient context, not because
    today's rules read it.
    """
    import threading

    from app.auth.context import use_user

    def _run() -> None:
        try:
            from app.integrations.tasks.reminders_inlet import tick_once

            db = get_db()
            with db.session() as session, use_user(user_id):
                result = tick_once(session)
                if result.get("completed") or result.get("captured") or result.get("pushed"):
                    logger.info(
                        "Reactive reminders-inlet tick (user_id=%d): %s",
                        user_id, result,
                    )
        except Exception:
            logger.exception("Reactive reminders-inlet tick failed (user_id=%d)", user_id)

    threading.Thread(target=_run, name="reminders-inlet-reactive", daemon=True).start()


# ---------------------------------------------------------------------------
# Health push (Apple Health import)
# ---------------------------------------------------------------------------

class HealthDailyMetric(BaseModel):
    date: str
    metric_type: str
    value: float


class HealthWorkout(BaseModel):
    uid: str
    workout_type: str
    start_time: str
    end_time: str
    duration_seconds: float = 0.0
    distance_km: float = 0.0
    active_energy_kcal: float = 0.0
    avg_heart_rate_bpm: float = 0.0


class HealthSleepSession(BaseModel):
    uid: str
    start_time: str
    end_time: str
    stage: str
    duration_hours: float = 0.0


class HealthPushRequest(BaseModel):
    daily_metrics: list[HealthDailyMetric] = []
    workouts: list[HealthWorkout] = []
    sleep_sessions: list[HealthSleepSession] = []


@router.post("/health/push")
def health_push(
    payload: HealthPushRequest,
    user: User = Depends(get_current_user),
) -> dict:
    """Bulk-upsert Apple Health metrics, workouts, and sleep sessions."""
    from app.integrations.apple_health.sync import sync_from_push as health_sync

    db = get_db()
    with db.session() as session:
        count = health_sync(
            [m.model_dump() for m in payload.daily_metrics],
            [w.model_dump() for w in payload.workouts],
            [s.model_dump() for s in payload.sleep_sessions],
            user_id=user.id,
            session=session,
        )

    # Reflect the push as a sync event so the alerting layer sees that
    # apple_health is alive. Without this, sync_state.apple_health.last_sync_at
    # never advances and the freshness check reports the integration as dead.
    from app.scheduler import _update_sync_state
    _update_sync_state("apple_health", status="ok", trigger="push")

    return {"ok": True, "count": count}


# ---------------------------------------------------------------------------
# Logs push (client log shipping)
# ---------------------------------------------------------------------------

class LogEntry(BaseModel):
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    level: str
    logger: str
    message: str


class LogsPushRequest(BaseModel):
    client_version: str = ""
    entries: list[LogEntry]


@router.post("/logs/push")
def logs_push(
    payload: LogsPushRequest,
    user: User = Depends(get_current_user),
) -> dict:
    """Append client log entries to `client_logs`."""
    if not payload.entries:
        return {"ok": True, "count": 0}

    from datetime import datetime, timezone
    from app.models.clients import ClientLog

    db = get_db()
    with db.session() as session:
        for entry in payload.entries:
            try:
                ts = datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                ts = datetime.now(timezone.utc)
            session.add(ClientLog(
                user_id=user.id,
                client_version=payload.client_version or None,
                level=entry.level,
                logger_name=entry.logger,
                message=entry.message[:5000],
                logged_at=ts,
            ))
        session.commit()

    return {"ok": True, "count": len(payload.entries)}


# ---------------------------------------------------------------------------
# AI usage ledger ingest (lios W2 chunk 1)
# ---------------------------------------------------------------------------
#
# "One place to see all the cloud and local AI usage" (the AI Broker plan)
# means callers outside this process — comar-hub's speech bridge, scribe,
# eventually Home Assistant — need a way to report a call in without a
# database dependency of their own. This is that route. Nothing in this repo
# calls it yet (in-process callers use app.services.ai_ledger.record()
# directly, which is cheaper than a round-trip HTTP call to yourself); it
# exists now so the later chunks that DO call it (comar-hub, scribe) have a
# stable contract to build against.
#
# Bearer-authenticated like every other v1 route — a stray unauthenticated
# usage-ingest endpoint would let anyone inflate (or deflate, by never
# calling it) the one number this whole design exists to make trustworthy.

class AiUsagePush(BaseModel):
    provider: str
    model: str
    kind: str = Field(..., description="chat | embedding | stt | tts | vision | prediction")
    caller: str
    role: str | None = None
    units_in: int = 0
    units_out: int = 0
    reasoning_units: int = 0
    seconds: float | None = None
    latency_ms: int | None = None
    cost_usd: float | None = Field(
        default=None,
        description="NULL means unknown, never free — a local call should send 0.0 explicitly.",
    )
    input_rate: float | None = None
    output_rate: float | None = None
    ok: bool = True
    error: str | None = None


@router.post("/ai/usage")
def ai_usage_push(
    payload: AiUsagePush,
    _user: User = Depends(get_current_user),
) -> dict:
    """Ingest one AI usage row from an external reporter (comar-hub, scribe, HA).

    Validates `kind` against the closed set rather than accepting anything —
    an unknown kind here is almost always a caller-side typo, and rejecting
    it loudly at ingest is cheaper to debug than a silently uncategorised
    row discovered later. Delegates to `app.services.ai_ledger.record()`,
    which is itself fire-and-forget — so this endpoint's own latency is just
    validation plus an in-memory enqueue, not a database round-trip.
    """
    from app.services import ai_ledger

    if payload.kind not in ai_ledger.VALID_KINDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown kind {payload.kind!r} — "
                f"must be one of {sorted(ai_ledger.VALID_KINDS)}"
            ),
        )

    ai_ledger.record(
        provider=payload.provider,
        model=payload.model,
        kind=payload.kind,
        caller=payload.caller,
        role=payload.role,
        units_in=payload.units_in,
        units_out=payload.units_out,
        reasoning_units=payload.reasoning_units,
        seconds=payload.seconds,
        latency_ms=payload.latency_ms,
        cost_usd=payload.cost_usd,
        input_rate=payload.input_rate,
        output_rate=payload.output_rate,
        ok=payload.ok,
        error=payload.error,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Events (SSE)
# ---------------------------------------------------------------------------

async def _notify_dashboard(kind: str, **fields: Any) -> None:
    """Best-effort push to the dashboard's own SSE stream (issue #141).

    Never raises into a daemon-facing code path — a browser tab that isn't
    open, or isn't listening, must not affect the bearer-authenticated
    stream this rides alongside. Async — call directly from a coroutine
    (e.g. `events_stream`, itself already on the event loop).
    """
    try:
        await stream_manager.publish(
            {"type": kind, **fields}, target_user=DASHBOARD_CHANNEL_USER,
        )
    except Exception:
        logger.exception("dashboard notify failed for %s", kind)


def _notify_dashboard_from_thread(kind: str, **fields: Any) -> None:
    """Same as `_notify_dashboard`, callable from a sync route (FastAPI runs
    a plain `def` endpoint in a worker thread — see `heartbeat` below —
    so publishing has to hop back onto the loop `stream_manager.loop` holds,
    the same pattern `apple_reminders/commands.py::dispatch_command` uses.
    Fire-and-forget: unlike that dispatch path this isn't waiting on an ack,
    so it doesn't block on the future's result.
    """
    loop = stream_manager.loop
    if loop is None:
        return
    asyncio.run_coroutine_threadsafe(_notify_dashboard(kind, **fields), loop)


@router.get("/events")
async def events_stream(user: User = Depends(get_current_user)) -> EventSourceResponse:
    """Server→client event stream.

    Subscribes to `stream_manager` for this user and re-emits each event
    as an SSE message. Clients should reconnect with exponential backoff
    on disconnect (sse-starlette sets `Last-Event-ID` automatically).

    Event shape: `data: <json>\\n\\n` where `<json>` is the dict published
    via `stream_manager.publish(...)`.

    Connect/disconnect here also pushes a `daemon_connection` event onto
    the dashboard's own stream (`DASHBOARD_CHANNEL_USER`, consumed by
    `GET /api/system/events`) — this IS the "is a daemon live right now"
    signal `system_alerts`' `daemon_status.sse_connected` already reads via
    `stream_manager.connected_users()`, just pushed instead of polled.
    """
    queue = await stream_manager.subscribe(user.name, channel="sse")
    logger.info("SSE: subscriber %s connected", user.name)
    await _notify_dashboard("daemon_connection", user=user.name, connected=True)

    async def _generator():
        try:
            # Send a hello event so the client knows the stream is live.
            yield {"event": "hello", "data": json.dumps({"user": user.name})}

            # Drain any command queued while nobody was subscribed. THIS is the
            # moment "no client connected" stops being true, so it is the right
            # place — a periodic cron would work too but would leave a write
            # sitting for up to its interval after the fix arrived.
            #
            # After the hello, not before: the drain re-dispatches over this very
            # stream, and publishing into a queue whose consumer hasn't started
            # yielding yet is how you lose the replay you just did.
            #
            # Bounded and best-effort by design — see `commands.drain_pending`.
            # A drain that raised here would break the subscribe it is attached
            # to, turning a lost write into a client that cannot connect at all.
            try:
                from app.plugin.capabilities import get_capability

                reminders = get_capability("reminders.query")
                drained = await asyncio.to_thread(
                    reminders.drain_pending_commands,
                    user_id=user.id,
                    user_name=user.name,
                )
                if drained.get("replayed") or drained.get("expired"):
                    logger.info("SSE: drained for %s: %s", user.name, drained)
            except Exception:
                logger.exception("SSE: pending-command drain failed for %s", user.name)

            while True:
                event = await queue.get()
                yield {"event": event.get("type", "message"), "data": json.dumps(event)}
        except asyncio.CancelledError:
            raise
        finally:
            await stream_manager.unsubscribe(user.name, channel="sse", queue=queue)
            logger.info("SSE: subscriber %s disconnected", user.name)
            # Another tab/token for the same user may still be subscribed
            # (multi-subscriber-per-key, see stream_manager's own
            # docstring) — only announce "gone" once nothing is left.
            if user.name not in stream_manager.connected_users():
                await _notify_dashboard("daemon_connection", user=user.name, connected=False)

    return EventSourceResponse(_generator())


# ---------------------------------------------------------------------------
# Syncthing pairing
# ---------------------------------------------------------------------------
#
# The Mac's installer starts a local Syncthing, reads its device ID via REST,
# and POSTs it here. The server's Syncthing is configured by adding the new
# device + sharing the `vault` folder with it. The Mac side has auto-accept
# folders set for the server, so the share lands automatically.
#
# Idempotent: re-POSTing the same device ID is a no-op (Syncthing's REST
# rejects duplicates with a clear status; we treat that as success).
#
# Device IDs look like XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX
# — 7-char groups separated by hyphens.

import re as _re
import httpx as _httpx

_DEVICE_ID_RE = _re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")


class SyncthingPairBody(BaseModel):
    device_id: str = Field(..., description="The Mac's Syncthing device ID.")
    device_name: str = Field("", description="Optional human label for the Syncthing UI.")


def _user_folder_ids(user_name: str) -> list[str]:
    """The Syncthing folder IDs this user's devices get paired with.

    Each user gets one folder: vault-<user_name> — their personal vault.
    (Single-user vaults; the cross-user vault-shared folder was retired.)
    """
    return [f"vault-{user_name}"]


async def _share_folder_with(client: "_httpx.AsyncClient", base: str, headers: dict, folder_id: str, device_id: str) -> None:
    """Idempotently append `device_id` to a folder's share list."""
    rf = await client.get(f"{base}/rest/config/folders/{folder_id}", headers=headers)
    if rf.status_code == 404:
        raise HTTPException(503, f"Server Syncthing folder {folder_id!r} not configured")
    if rf.status_code != 200:
        raise HTTPException(502, f"Syncthing fetch-folder {folder_id!r} returned {rf.status_code}")
    folder = rf.json()
    existing = {d["deviceID"] for d in folder.get("devices", [])}
    if device_id in existing:
        return
    folder["devices"].append({"deviceID": device_id})
    rs = await client.put(
        f"{base}/rest/config/folders/{folder_id}",
        headers=headers, json=folder,
    )
    if rs.status_code not in (200, 204):
        logger.error("Syncthing share-folder %s failed: %s %s", folder_id, rs.status_code, rs.text)
        raise HTTPException(502, f"Syncthing share-folder {folder_id!r} returned {rs.status_code}")


@router.post("/syncthing/pair")
async def syncthing_pair(
    body: SyncthingPairBody,
    user: User = Depends(get_current_user),
) -> dict:
    """Register a new device with the server's Syncthing + share this user's
    folder (`vault-<user>`).

    Bearer-auth-scoped: a user can only pair their own devices into their own
    folder. Sam's bearer cannot cause her device to be added to `vault-alex`
    because we never enumerate folders the caller doesn't own.
    """
    if not _DEVICE_ID_RE.match(body.device_id):
        raise HTTPException(400, "Malformed Syncthing device ID")
    if not settings.syncthing_api_key:
        raise HTTPException(503, "Server Syncthing not configured (no API key)")

    label = body.device_name or f"{user.name}-mac"
    base = settings.syncthing_url.rstrip("/")
    headers = {"X-API-Key": settings.syncthing_api_key}
    folder_ids = _user_folder_ids(user.name)

    async with _httpx.AsyncClient(timeout=10.0) as client:
        # Idempotently add (or refresh) the device entry.
        device_payload = {
            "deviceID": body.device_id,
            "name": label,
            "addresses": ["dynamic"],
            "compression": "metadata",
            "introducer": False,
            "autoAcceptFolders": False,
        }
        r = await client.put(
            f"{base}/rest/config/devices/{body.device_id}",
            headers=headers, json=device_payload,
        )
        if r.status_code not in (200, 204):
            logger.error("Syncthing add-device failed: %s %s", r.status_code, r.text)
            raise HTTPException(502, f"Syncthing add-device returned {r.status_code}")

        for fid in folder_ids:
            await _share_folder_with(client, base, headers, fid, body.device_id)

    logger.info(
        "Syncthing paired: user=%s device_id=%s label=%s folders=%s",
        user.name, body.device_id, label, folder_ids,
    )
    return {
        "ok": True,
        "folders": folder_ids,
        "server_device_id_via": "GET /api/v1/syncthing/server-id",
    }


@router.get("/syncthing/server-id")
async def syncthing_server_id(user: User = Depends(get_current_user)) -> dict:
    """Return the server's Syncthing device ID + the folder IDs this user
    needs to mirror on their Mac. The Mac side uses this to configure one
    Syncthing folder share: vault-<user> (nested at vault/)."""
    if not settings.syncthing_api_key:
        raise HTTPException(503, "Server Syncthing not configured (no API key)")
    base = settings.syncthing_url.rstrip("/")
    headers = {"X-API-Key": settings.syncthing_api_key}
    async with _httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{base}/rest/system/status", headers=headers)
        if r.status_code != 200:
            raise HTTPException(502, f"Syncthing returned {r.status_code}")
        return {
            "device_id": r.json()["myID"],
            "folders": _user_folder_ids(user.name),
            "sync_address": "tcp://ubuntudockerbox.tail78010b.ts.net:22000",
        }
