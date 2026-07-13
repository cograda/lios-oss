"""Comar daemon — runs MCP server, HTTP server client, and background tasks.

This is the main entry point for the long-running client process.
Started by `comar daemon` or the launchd agent.

V3 transport: HTTPS + JSON for tools and pushes, SSE for server→client signals.
No gRPC, no protobuf, no MLX (the transcriber lives in a separate repo now).
"""

import asyncio
import logging
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from comar.config import load_config
from comar.server_client import ServerClient
from comar.mcp_server import create_mcp_app, refresh_server_tools
from comar.prompts import PromptStore
from comar.remote_logging import RemoteLogHandler
from comar.supervisor import TaskSupervisor
from comar.vault_watcher import start_vault_watcher

logger = logging.getLogger("comar")

LOG_DIR = Path.home() / "Library" / "Logs" / "comar"


def _setup_logging() -> None:
    """Configure logging with rotating file handler + console."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "daemon.log"

    fmt = logging.Formatter("%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


async def run_daemon():
    """Main daemon loop."""
    _setup_logging()

    config = load_config()
    if not config.user:
        logger.error("No user configured. Run `comar setup` first.")
        return
    if not config.server.token:
        logger.error("No server token configured. Run `comar setup` first.")
        return

    logger.info(f"Starting comar daemon (user={config.user})")

    # 1. HTTP client + initial heartbeat
    server_client = ServerClient(config)

    remote_handler = RemoteLogHandler(server_client)
    remote_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S",
    ))
    logging.getLogger().addHandler(remote_handler)

    initial_hb = None
    try:
        initial_hb = await asyncio.to_thread(server_client.heartbeat)
        server_client.mark_connected()
        logger.info("Server connected")
    except Exception as e:  # noqa: BLE001
        server_client.mark_disconnected()
        logger.warning(f"Server heartbeat failed: {e} — continuing with local tools only")

    # 2. Vault watcher (before MCP so handler is available for health endpoint)
    vault_path = Path(config.vault.path)
    vault_observer = None
    vault_handler = None
    if vault_path.is_dir():
        vault_observer, vault_handler = start_vault_watcher(vault_path, server_client)
    else:
        logger.warning(f"Vault path not found: {vault_path} — watcher disabled")

    # 2b. EventKit reminder store (macOS only)
    reminder_store = None
    try:
        from comar.eventkit import ReminderStore
        reminder_store = ReminderStore(
            loop=asyncio.get_event_loop(),
            source_emails=config.reminders.source_emails,
        )
        if reminder_store.is_available:
            logger.info("EventKit reminder store initialized")
        else:
            logger.warning("EventKit access not granted — reminders disabled")
            reminder_store = None
    except ImportError:
        logger.info("PyObjC/EventKit not available — reminders disabled")
    except Exception:
        logger.exception("Failed to initialize EventKit — reminders disabled")

    # 2c. Apple Health — data arrives via `comar import-health` (Health Auto Export JSON)
    #     macOS has no HealthKit data store, so there's no live push loop.

    # 2d. Load MCP prompt definitions
    from comar.config import CONFIG_DIR
    prompt_store = PromptStore()
    prompts_dir = CONFIG_DIR / "prompts"
    prompt_store.load_from_dir(prompts_dir)

    # 3. Background-task supervision (created before the MCP app so the
    # local /health endpoint can report per-task liveness)
    shutdown_event = asyncio.Event()
    supervisor = TaskSupervisor(shutdown_event)

    # 3b. Create MCP server (merges local + proxied tools + prompts)
    mcp_app, mcp_server, tool_store = create_mcp_app(
        config, server_client, vault_handler=vault_handler,
        reminder_store=reminder_store, prompt_store=prompt_store,
        supervisor=supervisor,
    )

    # 4. Background tasks

    async def _reminders_loop():
        """Push reminders to server on EventKit change or every 30s fallback."""
        if reminder_store is None:
            logger.info("Reminders loop disabled (no EventKit store)")
            return

        last_push_hash = None
        while not shutdown_event.is_set():
            supervisor.beat("reminders")
            try:
                await asyncio.wait_for(reminder_store.change_event.wait(), timeout=30)
                reminder_store.change_event.clear()
                await asyncio.sleep(2)  # debounce burst changes
            except asyncio.TimeoutError:
                pass

            if shutdown_event.is_set():
                break

            if not server_client.is_connected:
                continue

            try:
                reminders = await asyncio.to_thread(reminder_store.read_all_reminders)
                current_hash = hash(tuple(
                    (r["uid"], r["summary"], r["completed"], r.get("due_date"))
                    for r in sorted(reminders, key=lambda r: r["uid"])
                ))
                if current_hash != last_push_hash:
                    await asyncio.to_thread(server_client.push_reminders, reminders)
                    last_push_hash = current_hash
                    logger.info(f"Pushed {len(reminders)} reminders to server")
                # Always report bridge liveness, even when nothing changed —
                # otherwise `users.reminders_verified_at` stays put during
                # quiet periods and data_freshness flags a false alarm.
                await asyncio.to_thread(server_client.report_reminders_verified)
            except Exception:
                logger.exception("Reminders push/verify failed")

    async def _heartbeat_loop():
        """Poll /api/v1/heartbeat every 5 minutes — covers reconnect, auto-update, prompt sync."""
        # Apply initial heartbeat immediately, then loop.
        if initial_hb:
            await _apply_heartbeat(initial_hb, prompt_store, remote_handler, config, server_client)

        while not shutdown_event.is_set():
            supervisor.beat("heartbeat")
            try:
                hb = await asyncio.to_thread(
                    server_client.heartbeat, supervisor.health()
                )
                was_disconnected = not server_client.is_connected
                server_client.mark_connected()

                if was_disconnected:
                    logger.info("Server connection restored — refreshing tools")
                    await asyncio.to_thread(refresh_server_tools, tool_store, server_client)

                await _apply_heartbeat(hb, prompt_store, remote_handler, config, server_client)

            except Exception as e:  # noqa: BLE001
                logger.warning(f"Heartbeat failed: {e}")
                server_client.mark_disconnected()
                try:
                    success = await asyncio.to_thread(server_client.reconnect)
                    if success:
                        await asyncio.to_thread(refresh_server_tools, tool_store, server_client)
                except Exception:
                    logger.warning("Reconnection attempt failed")

            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass

    async def _events_loop():
        """Consume server→client SSE events. Reconnect with backoff on failure."""
        backoff = [5, 10, 30, 60]
        attempt = 0

        while not shutdown_event.is_set():
            supervisor.beat("events")
            if not server_client.is_connected:
                await asyncio.sleep(5)
                continue

            try:
                logger.info("SSE: connecting to /api/v1/events")
                attempt = 0

                # iter_lines is sync; pull each event in a thread.
                stream = await asyncio.to_thread(_open_event_iter, server_client)
                while not shutdown_event.is_set():
                    supervisor.beat("events")
                    event = await asyncio.to_thread(_next_event, stream)
                    if event is None:
                        logger.info("SSE: server closed stream")
                        break
                    await _handle_server_event(event, prompt_store, server_client, reminder_store)
            except Exception as e:  # noqa: BLE001
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.debug("SSE: %s — retrying in %ds", type(e).__name__, wait)
                attempt += 1
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass

    # 5. Signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: shutdown_event.set())

    # 6. Start MCP + background tasks under supervision (ISS-001: a loop
    # that dies from an unhandled exception is restarted with backoff
    # instead of leaving a silently half-dead process).
    logger.info(f"MCP server listening on localhost:{config.mcp_port}")
    tasks = [
        supervisor.start(
            "mcp",
            lambda: _run_uvicorn(mcp_app, port=config.mcp_port, name="mcp"),
            beats=False,
        ),
        supervisor.start("reminders", _reminders_loop),
        supervisor.start("heartbeat", _heartbeat_loop),
        supervisor.start("events", _events_loop),
    ]

    try:
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        if vault_observer:
            vault_observer.stop()
            vault_observer.join(timeout=5)
        if reminder_store:
            reminder_store.shutdown()
        remote_handler.close()
        server_client.close()
        logger.info("Daemon stopped")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_event_iter(server_client):
    """Open the SSE stream and return an iterator. Runs in a thread."""
    return server_client.open_event_stream()


def _next_event(stream):
    """Pull the next event dict, or None if the iterator is exhausted."""
    try:
        return next(stream)
    except StopIteration:
        return None


async def _handle_server_event(
    event: dict,
    prompt_store: PromptStore,
    server_client: ServerClient,
    reminder_store=None,
) -> None:
    """Process a single SSE event from the server."""
    event_type = event.get("type")
    if event_type == "prompt_update":
        new_hash = event.get("prompt_set_hash", "")
        if new_hash and new_hash != prompt_store.prompt_set_hash():
            try:
                server_prompts = await asyncio.to_thread(server_client.list_prompts)
                updated = prompt_store.sync_from_server(server_prompts)
                if updated:
                    logger.info(f"SSE: synced {updated} prompts")
            except Exception:
                logger.exception("SSE: prompt sync failed")
    elif event_type == "eventkit_command":
        await _handle_eventkit_command(event, server_client, reminder_store)
    elif event_type == "hello":
        logger.debug("SSE: hello received")
    else:
        logger.debug("SSE: unhandled event type %s", event_type)


async def _handle_eventkit_command(
    event: dict, server_client: ServerClient, reminder_store,
) -> None:
    """Execute a server-dispatched EventKit command and ack back.

    D.5: server publishes {type: 'eventkit_command', command_id, action, args}.
    We run the EventKit op in a thread (lock-guarded inside ReminderStore),
    then POST result-or-error to /api/v1/reminders/commands/{id}/done.
    """
    command_id = event.get("command_id")
    action = event.get("action")
    args = event.get("args") or {}
    if command_id is None or action is None:
        logger.warning("SSE: malformed eventkit_command: %s", event)
        return

    if reminder_store is None or not reminder_store.is_available:
        try:
            await asyncio.to_thread(
                server_client.ack_reminder_command,
                command_id, error="EventKit not available on this client",
            )
        except Exception:
            logger.exception("SSE: failed to ack unavailable EventKit")
        return

    try:
        if action == "add":
            uid = await asyncio.to_thread(
                reminder_store.add_reminder,
                args.get("summary", ""),
                args.get("list", "Reminders"),
                args.get("due_date"),
                args.get("priority", "none"),
                args.get("notes"),
                args.get("account_email"),
            )
            result = {"uid": uid}
        elif action == "complete":
            uid = args.get("uid", "")
            success = await asyncio.to_thread(reminder_store.complete_reminder, uid)
            if not success:
                raise RuntimeError(f"Reminder {uid} not found")
            result = {"uid": uid, "completed": True}
        else:
            raise ValueError(f"Unknown action: {action}")
    except Exception as e:  # noqa: BLE001
        logger.exception("SSE: eventkit_command %s failed", action)
        try:
            await asyncio.to_thread(
                server_client.ack_reminder_command, command_id, error=str(e),
            )
        except Exception:
            logger.exception("SSE: failed to ack failure for cmd %d", command_id)
        return

    try:
        await asyncio.to_thread(
            server_client.ack_reminder_command, command_id, result=result,
        )
    except Exception:
        logger.exception("SSE: failed to ack success for cmd %d", command_id)


async def _apply_heartbeat(
    hb: dict,
    prompt_store: PromptStore,
    remote_handler,
    config,
    server_client: ServerClient,
) -> None:
    """React to a heartbeat response: prompt sync + auto-update.

    Prompt updates also flow via SSE `prompt_update` events; this is the
    backstop when the SSE stream is disconnected.
    """
    server_hash = hb.get("prompt_set_hash", "")
    if server_hash and server_hash != prompt_store.prompt_set_hash():
        try:
            server_prompts = await asyncio.to_thread(server_client.list_prompts)
            updated = prompt_store.sync_from_server(server_prompts)
            if updated:
                logger.info(f"Heartbeat: synced {updated} prompts")
        except Exception:
            logger.exception("Heartbeat: prompt sync failed")

    # Auto-update: download new wheel if newer.
    latest_version = hb.get("latest_client_version", "")
    if config.auto_update and latest_version:
        from comar.updater import check_for_update, download_and_install, restart_daemon
        from comar.server_client import _get_version

        current = _get_version()
        if check_for_update(current, latest_version):
            logger.info(f"Update available: {current} → {latest_version}")
            checksum = hb.get("latest_client_checksum", "")
            success = await asyncio.to_thread(
                download_and_install, config.server.url, checksum, config.allow_insecure_updates,
            )
            if success:
                logger.info("Update installed — restarting daemon")
                remote_handler.flush()
                await asyncio.to_thread(restart_daemon)


async def _run_uvicorn(app, port: int, name: str):
    """Run a Starlette/ASGI app with uvicorn in an asyncio task."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
