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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.auth.client_token import get_current_user
from app.config import settings
from app.db import get_db
from app.models.users import User
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

    Rendered per user from `app.prompts.commands` (daily-note, add-task,
    find, triage, lock-in, week-ahead) — the single source of truth for
    this command set. Used by the installer to populate a new machine's
    `~/Comar/.claude/commands/` and by the daemon's optional startup
    refresh. Alex's copies are delivered to `vault/.claude/commands/` from
    the same templates by `scripts/render_commands.py` rather than by this
    endpoint, because his machine has the repo checked out.
    """
    from app.prompts.commands import render_claude_md, render_command_set
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
) -> JSONResponse:
    """Invoke a tool by name. Body is the raw JSON arguments object.

    Response shape mirrors the gRPC `CallToolResponse`: `{"ok": true, "result": ...}`
    or `{"ok": false, "error": "..."}`. The tool's own JSON return is
    embedded as `result` (parsed) so callers don't double-decode.

    Thin adapter over the shared `dispatch_tool()` chokepoint (V4 chunk
    2.1) — auth is resolved above via `Depends(get_current_user)`
    (transport-specific), then handed to `dispatch_tool` as an
    already-authenticated `User`.
    """
    from app.plugin.dispatch import dispatch_tool
    from app.plugin.registry import get_tool_handler

    if get_tool_handler(name) is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")

    try:
        body = await request.body()
        arguments: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

    outcome = await dispatch_tool(
        name, arguments, user,
        transport="http",
        source_ip=request.client.host if request.client else None,
    )

    if outcome.status == "timeout":
        return JSONResponse({"ok": False, "error": json.loads(outcome.content)["error"]}, status_code=504)
    if outcome.is_error:
        return JSONResponse({"ok": False, "error": json.loads(outcome.content)["error"]}, status_code=500)

    # Tool handlers conventionally return a JSON-encoded string. Parse it
    # so the HTTP response carries structured data, not a string-of-JSON.
    parsed: Any = outcome.content
    try:
        parsed = json.loads(outcome.content)
    except (json.JSONDecodeError, TypeError):
        parsed = outcome.content  # leave as raw string if not JSON

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
            from app.models.users import User
            from app.services import vault_paths

            db = get_db()
            with db.session() as session, use_user(user_id):
                user = session.get(User, user_id)
                if not user:
                    return
                vault_dir = vault_paths.user_vault_path(user.name)
                if not vault_dir.is_dir():
                    return
                # sync_backlogs is a plain blocking function; we're already
                # off the event loop in this dedicated worker thread, so
                # call it directly (no asyncio.run() needed).
                result = sync_backlogs(session, str(vault_dir), user_id)
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
