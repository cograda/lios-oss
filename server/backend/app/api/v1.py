"""V3 client HTTP API — `/api/v1/*`.

Endpoints:
  GET  /api/v1/tools                  — list registered MCP tools
  POST /api/v1/tools/{name}           — invoke a registered MCP tool
  POST /api/v1/vault/push             — push a single vault file (write + index)
  POST /api/v1/reminders/push         — push reminders snapshot
  GET  /api/v1/events                 — Server-Sent Events stream (server→client)

All endpoints require an `Authorization: Bearer <client_token>` header
validated against the `client_tokens` table. The authenticated user is
exposed to handlers via `Depends(get_current_user)`.
"""

import asyncio
import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.auth.client_token import get_current_user
from app.config import settings
from app.db import get_db
from app.errors import PermanentError
from app.models.users import User
from app.services.tool_calls import record_tool_call
from app.stream_manager import stream_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["v1"])


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

@router.get("/heartbeat")
def heartbeat(
    client_version: str = "",
    task_health: str = "",
    user: User = Depends(get_current_user),
) -> dict:
    """Lightweight liveness probe with two-tier sync hashes.

    Returns the latest client wheel version + checksum and the prompt set
    hash so the client can detect updates with a single poll.
    """
    from datetime import datetime, timezone
    from app.models.clients import ClientToken
    from app.prompts.registry import get_prompt_set_hash

    # Update client_version on this user's most recently active token.
    # last_seen_at was already touched by get_current_user.
    db = get_db()
    with db.session() as session:
        row = (
            session.query(ClientToken)
            .filter_by(user_id=user.id, is_active=True)
            .order_by(ClientToken.last_seen_at.desc().nullslast())
            .first()
        )
        if row and client_version:
            row.client_version = client_version
        if row and task_health:
            row.task_health = task_health[:4000]
        if row and (client_version or task_health):
            session.commit()

    latest_version, latest_checksum = _latest_client_info()

    return {
        "ok": True,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "latest_client_version": latest_version,
        "latest_client_checksum": latest_checksum,
        "prompt_set_hash": get_prompt_set_hash(),
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


def _latest_client_info() -> tuple[str, str]:
    """Read latest client wheel version + SHA256. Returns ('', '') if none."""
    import hashlib
    import re
    from pathlib import Path

    for base in [Path("/app/client-dist"), Path(__file__).resolve().parents[3] / "client-dist"]:
        if not base.is_dir():
            continue
        wheels = sorted(base.glob("comar_client-*.whl"), reverse=True)
        if not wheels:
            continue
        m = re.search(r"comar_client-([^-]+)-", wheels[0].name)
        if not m:
            continue
        h = hashlib.sha256()
        with open(wheels[0], "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return m.group(1), h.hexdigest()
    return "", ""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@router.get("/prompts")
def list_prompts(_user: User = Depends(get_current_user)) -> list[dict]:
    """Return all server-side prompt definitions for client mirroring."""
    from app.prompts.registry import get_all_prompts
    return get_all_prompts()


# ---------------------------------------------------------------------------
# MCP server instructions (preamble for the model)
# ---------------------------------------------------------------------------

@router.get("/instructions")
def get_instructions(_user: User = Depends(get_current_user)) -> dict:
    """Return the canonical MCP instructions block.

    Single source of truth, mirrored to the local client so both
    server-side (/mcp/sse) and client-side (localhost:9400) MCP servers
    advertise the same preamble.
    """
    from app.mcp.instructions import COMAR_INSTRUCTIONS
    return {"instructions": COMAR_INSTRUCTIONS}


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
) -> JSONResponse:
    """Invoke a tool by name. Body is the raw JSON arguments object.

    Response shape mirrors the gRPC `CallToolResponse`: `{"ok": true, "result": ...}`
    or `{"ok": false, "error": "..."}`. The tool's own JSON return is
    embedded as `result` (parsed) so callers don't double-decode.
    """
    from app.mcp.server import _tool_handlers
    from app.services.freshness import ensure_fresh

    if name not in _tool_handlers:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")

    try:
        body = await request.body()
        arguments: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

    handler_fn, integration_name = _tool_handlers[name]

    # Pin user_id for the duration of this tool call so handlers can scope
    # reads to the authenticated user via current_user_id().
    from app.auth.context import use_user
    from app.mcp.server import bind_tool_call_id

    def _run() -> str:
        db = get_db()
        with db.session() as session, use_user(user.id):
            ensure_fresh(integration_name, session)
            return handler_fn(session, arguments)

    call_id = secrets.token_hex(4)
    start = time.monotonic()
    status = "ok"
    error_text: str | None = None
    with bind_tool_call_id(call_id):
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_run), timeout=60)
        except asyncio.TimeoutError:
            status = "timeout"
            error_text = f"Tool {name} timed out"
            logger.warning("Tool %s timed out (user=%s)", name, user.name)
            return JSONResponse({"ok": False, "error": error_text}, status_code=504)
        except PermanentError as e:
            # Bad credentials / config, not a bug worth a stack trace every
            # call — warn (no traceback spam) and return a structured error.
            status = "error"
            error_text = str(e)
            logger.warning("Tool %s failed (permanent, user=%s): %s", name, user.name, e)
            return JSONResponse({"ok": False, "error": error_text}, status_code=500)
        except Exception as e:  # noqa: BLE001
            status = "error"
            error_text = str(e)
            logger.exception("Tool %s failed (user=%s)", name, user.name)
            return JSONResponse({"ok": False, "error": error_text}, status_code=500)
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "tool=%s user=%s tool_call_id=%s duration_ms=%d status=%s",
                name, user.id, call_id, duration_ms, status,
            )
            await asyncio.to_thread(
                record_tool_call,
                name=name, user_id=user.id, duration_ms=duration_ms,
                status=status, error=error_text, tool_call_id=call_id,
            )

    # Tool handlers conventionally return a JSON-encoded string. Parse it
    # so the HTTP response carries structured data, not a string-of-JSON.
    parsed: Any = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            parsed = result  # leave as raw string if not JSON

    return JSONResponse({"ok": True, "result": parsed})


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
    """Write a single vault file to disk and re-index it."""
    from pathlib import Path
    from app.integrations.obsidian.sync import index_single_file

    vault_path = settings.obsidian_vault_path
    if not vault_path:
        raise HTTPException(status_code=503, detail="Vault path not configured")

    # Containment check — payload.path is bearer-authenticated input but a
    # value like "../../../app/main.py" would otherwise let any client overwrite
    # arbitrary container files (RCE-equivalent on the next reload).
    rel = payload.path.lstrip("/")
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise HTTPException(status_code=400, detail="path escapes vault")
    vault_root = Path(vault_path).resolve()
    full = (vault_root / rel).resolve()
    if not str(full).startswith(str(vault_root) + "/") and full != vault_root:
        raise HTTPException(status_code=400, detail="path escapes vault")

    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(payload.content, encoding="utf-8")

    db = get_db()
    with db.session() as session:
        index_single_file(session, payload.path, payload.content, payload.file_hash)

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
        # Reactive backlog sync — pinned to the pushing user. Threads do not
        # propagate ContextVars, so we must rebind inside the worker.
        _trigger_backlog_sync(user.id)

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


def _trigger_backlog_sync(user_id: int) -> None:
    """Fire-and-forget vault↔Reminders backlog sync after meaningful changes.

    `user_id` is the pushing user's id, snapshotted before spawning the thread.
    `sync_backlogs` queries reminders/vault scoped to this user, so binding the
    ContextVar inside the worker is required (threading.Thread does not
    propagate ContextVars).
    """
    import threading

    from app.auth.context import use_user

    def _run() -> None:
        try:
            from app.integrations.apple_reminders.backlog_sync import sync_backlogs

            vault_path = settings.obsidian_vault_path
            if not vault_path:
                return
            db = get_db()
            with db.session() as session, use_user(user_id):
                # sync_backlogs is a plain blocking function; we're already
                # off the event loop in this dedicated worker thread, so
                # call it directly (no asyncio.run() needed).
                result = sync_backlogs(session, vault_path)
                if result.get("completed_in_vault"):
                    logger.info(
                        "Reactive backlog sync (user_id=%d): %d tasks marked done in vault",
                        user_id, result["completed_in_vault"],
                    )
        except Exception:
            logger.exception("Reactive backlog sync failed (user_id=%d)", user_id)

    threading.Thread(target=_run, name="backlog-sync-reactive", daemon=True).start()


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
# Events (SSE)
# ---------------------------------------------------------------------------

@router.get("/events")
async def events_stream(user: User = Depends(get_current_user)) -> EventSourceResponse:
    """Server→client event stream.

    Subscribes to `stream_manager` for this user and re-emits each event
    as an SSE message. Clients should reconnect with exponential backoff
    on disconnect (sse-starlette sets `Last-Event-ID` automatically).

    Event shape: `data: <json>\\n\\n` where `<json>` is the dict published
    via `stream_manager.publish(...)`.
    """
    queue = await stream_manager.subscribe(user.name, channel="sse")
    logger.info("SSE: subscriber %s connected", user.name)

    async def _generator():
        try:
            # Send a hello event so the client knows the stream is live.
            yield {"event": "hello", "data": json.dumps({"user": user.name})}
            while True:
                event = await queue.get()
                yield {"event": event.get("type", "message"), "data": json.dumps(event)}
        except asyncio.CancelledError:
            raise
        finally:
            await stream_manager.unsubscribe(user.name, channel="sse", queue=queue)
            logger.info("SSE: subscriber %s disconnected", user.name)

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
            "sync_address": "tcp://your-server.your-tailnet.ts.net:22000",
        }
