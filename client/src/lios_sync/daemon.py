"""Comar daemon — runs MCP server, HTTP server client, and background tasks.

This is the main entry point for the long-running client process.
Started by `lios-sync daemon` or the launchd agent.

V3 transport: HTTPS + JSON for tools and pushes, SSE for server→client signals.
No gRPC, no protobuf, no MLX (the transcriber lives in a separate repo now).
"""

import asyncio
import logging
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from lios_sync.config import load_config
from lios_sync.server_client import AuthError, ServerClient
from lios_sync.health_server import create_health_app
from lios_sync.remote_logging import RemoteLogHandler
from lios_sync.supervisor import TaskSupervisor
from lios_sync.vault_watcher import start_vault_watcher

logger = logging.getLogger("lios-sync")

LOG_DIR = Path.home() / "Library" / "Logs" / "lios"


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
        logger.error("No user configured. Run `lios-sync setup` first.")
        return
    if not config.server.token:
        logger.error("No server token configured. Run `lios-sync setup` first.")
        return

    logger.info(f"Starting lios-sync daemon (user={config.user})")

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
        await asyncio.to_thread(_refresh_commands, server_client, config)
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

    # 2a. Voice-memo watcher — the third and last macOS-only job, alongside
    # fsevents and EventKit. It only notices new recordings and uploads them;
    # classification and transcription are the server's (see voice_memos.py).
    # Opt-in via config: it reads a private container and each upload costs a
    # server-side transcription.
    voice_memo_observer = None
    voice_memo_handler = None
    # Tri-state, reported verbatim on /health and in the heartbeat. A bare
    # boolean here is what made this invisible for 19 days: `enabled = true` in
    # config with `voice_memo_watcher_active: false` on /health looked exactly
    # like "capture is off", because `false` meant both. 138 recordings on disk,
    # 110 eligible, 0 uploaded, no ledger file ever created, and nothing
    # anywhere reported a fault — `start_voice_memo_watcher()` returns None on
    # PermissionError, logs a warning and lets the daemon continue.
    #
    # "Off" and "broken" must not share a value. That is the whole fix.
    voice_memo_state = "disabled"
    voice_memo_detail = "voice_memos.enabled is false in config.toml"
    if config.voice_memos.enabled:
        try:
            from lios_sync.voice_memos import RECORDINGS_DIR, start_voice_memo_watcher

            started = start_voice_memo_watcher(
                server_client,
                config.voice_memos.recordings_path or RECORDINGS_DIR,
            )
            if started:
                voice_memo_observer, voice_memo_handler = started
                voice_memo_state = "active"
                voice_memo_detail = ""
            else:
                # Enabled and it did not start. Overwhelmingly this is Full Disk
                # Access: the *launchd agent* needs its own grant, separate from
                # the one your terminal already has, and nothing tells you which
                # of the two is missing.
                voice_memo_state = "failed"
                voice_memo_detail = (
                    "enabled but the watcher did not start — the launchd agent "
                    "most likely lacks Full Disk Access (a separate grant from "
                    "your terminal's). System Settings → Privacy & Security → "
                    "Full Disk Access."
                )
                logger.error(
                    "Voice-memo capture is ENABLED but the watcher did not start: %s",
                    voice_memo_detail,
                )
        except Exception as exc:
            # Never fatal: the vault watcher and reminders are the daemon's
            # primary jobs and must come up regardless. But it is a *fault*, not
            # a quiet absence — say so in the state, not only in the log.
            voice_memo_state = "failed"
            voice_memo_detail = f"watcher raised on startup: {exc}"
            logger.exception("Voice-memo watcher failed to start — continuing without it")
    else:
        logger.debug("Voice-memo capture disabled (set voice_memos.enabled in config.toml)")

    # 2b. EventKit reminder store (macOS only)
    reminder_store = None
    try:
        from lios_sync.eventkit import ReminderStore
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

    # 2c. Apple Health — data arrives via `lios-sync import-health` (Health Auto Export JSON)
    #     macOS has no HealthKit data store, so there's no live push loop.

    # 3. Background-task supervision (created before the health app so the
    # local /health endpoint can report per-task liveness)
    shutdown_event = asyncio.Event()
    supervisor = TaskSupervisor(shutdown_event)

    # 3b. Create the minimal health app (Phase 4: no more local MCP server —
    # Claude Code and every other consumer talk Streamable HTTP directly to
    # the comar server's /mcp/. This just answers "is the daemon alive?".)
    health_app = create_health_app(
        vault_handler=vault_handler,
        supervisor=supervisor,
        voice_memo_handler=voice_memo_handler,
        voice_memo_state=voice_memo_state,
        voice_memo_detail=voice_memo_detail,
    )

    def _task_health_with_watchers() -> dict:
        """`supervisor.health()` plus a synthetic entry for a *failed* watcher.

        Deliberately reuses the task-health shape rather than adding a parallel
        field, because the server already alerts on any entry carrying a
        `last_error` (`system/tools.py` axis 5). So a watcher that was enabled
        and did not start now surfaces as
        `task voice_memo_watcher unhealthy (last_error=…)` with **no server
        change at all**, and rides the existing notification ledger.

        Only reported when the state is `failed`. A watcher that is off by
        choice is not a fault, and alerting on it would be the mirror of the
        bug this fixes — noise that trains you to ignore the channel.
        """
        health = dict(supervisor.health())
        if voice_memo_state == "failed":
            health["voice_memo_watcher"] = {
                "alive_seconds_ago": None,
                "restarts": 0,
                "last_error": voice_memo_detail,
                "finished": True,
            }
        return health

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
        """Poll /api/v1/heartbeat every 5 minutes — covers reconnect + auto-update."""
        # Apply initial heartbeat immediately, then loop.
        if initial_hb:
            await _apply_heartbeat(initial_hb, remote_handler, config, server_client)

        while not shutdown_event.is_set():
            supervisor.beat("heartbeat")
            try:
                hb = await asyncio.to_thread(
                    server_client.heartbeat, _task_health_with_watchers()
                )
                was_disconnected = not server_client.is_connected
                server_client.mark_connected()

                if was_disconnected:
                    logger.info("Server connection restored")
                    await asyncio.to_thread(_refresh_commands, server_client, config)

                # We are talking to *something*, but possibly the fallback.
                # If the preferred endpoint (normally the LAN address) is back,
                # move onto it — otherwise every push from the sofa would keep
                # looping out over Tailscale and back in again. No-op when
                # already on the preferred endpoint, so this is free at home.
                await asyncio.to_thread(server_client.recheck_preferred)

                await _apply_heartbeat(hb, remote_handler, config, server_client)

            except AuthError as e:
                # Token rejected (likely expired) — distinct from a generic
                # connection failure so it's visible in the log at a glance.
                # Still loop rather than crash: the token could be replaced
                # (re-run installer) without a daemon restart, and this cycle
                # already only runs every 5 minutes.
                logger.error(f"Heartbeat failed: {e}")
                server_client.mark_disconnected()
                try:
                    success = await asyncio.to_thread(server_client.reconnect)
                    if success:
                        logger.info("Server connection restored")
                except Exception:
                    logger.warning("Reconnection attempt failed")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Heartbeat failed: {e}")
                server_client.mark_disconnected()
                try:
                    success = await asyncio.to_thread(server_client.reconnect)
                    if success:
                        logger.info("Server connection restored")
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
                    await _handle_server_event(event, server_client, reminder_store)
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

    # 6. Start health app + background tasks under supervision (ISS-001: a
    # loop that dies from an unhandled exception is restarted with backoff
    # instead of leaving a silently half-dead process).
    logger.info(f"Health endpoint listening on localhost:{config.health_port}")
    tasks = [
        supervisor.start(
            "health",
            lambda: _run_uvicorn(health_app, port=config.health_port, name="health"),
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
        if voice_memo_observer:
            voice_memo_observer.stop()
            voice_memo_observer.join(timeout=5)
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
    server_client: ServerClient,
    reminder_store=None,
) -> None:
    """Process a single SSE event from the server."""
    event_type = event.get("type")
    if event_type == "eventkit_command":
        await _handle_eventkit_command(event, server_client, reminder_store)
    elif event_type == "hello":
        logger.debug("SSE: hello received")
    elif event_type == "prompt_update":
        # Retired in Phase 4 (2026-07-14): MCP prompt serving was only
        # reachable via the deleted local MCP app, which the canary proved
        # nothing was calling. Nothing left to sync.
        logger.debug("SSE: prompt_update received (no-op — prompt mirroring retired)")
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
    remote_handler,
    config,
    server_client: ServerClient,
) -> None:
    """React to a heartbeat response: auto-update.

    Prompt syncing was retired in Phase 4 (2026-07-14) along with the local
    MCP app — nothing on this Mac consumed the mirrored prompt files anymore.
    """
    # Auto-update: download new wheel if newer.
    latest_version = hb.get("latest_client_version", "")
    if config.auto_update and latest_version:
        from lios_sync.updater import check_for_update, download_and_install, restart_daemon
        from lios_sync.server_client import _get_version

        current = _get_version()
        if check_for_update(current, latest_version):
            logger.info(f"Update available: {current} → {latest_version}")
            checksum = hb.get("latest_client_checksum", "")
            # Pull the wheel from whichever endpoint we are actually on —
            # `config.server.urls[0]` may be the LAN address while the daemon
            # is currently reaching the server over Tailscale. The bearer is
            # required: wheel downloads are gated (V4 chunk 2.5).
            success = await asyncio.to_thread(
                download_and_install,
                server_client.base_url,
                checksum,
                config.server.token,
            )
            if success:
                logger.info("Update installed — restarting daemon")
                remote_handler.flush()
                await asyncio.to_thread(restart_daemon)


def _refresh_commands(server_client: ServerClient, config) -> None:
    """One-shot pull of CLAUDE.md + slash commands from the server.

    sam-live Phase 0 (0.3): keeps a non-technical family member's `~/Comar/`
    command set current without another Mac visit. Runs synchronously — call
    it via `asyncio.to_thread` — and never raises: this is a convenience
    refresh, not one of the daemon's primary jobs (EventKit / fsevents /
    voice memos), so a failure here must never affect those.

    Guards, in order:
    - `working_dir/.git` exists → this is the developer's Mac, where
      `vault.path.parent` is the dev repo root and CLAUDE.md is hand-written.
      Writing here would clobber it.
    - `working_dir/.claude/commands` isn't a directory → nothing the
      installer provisioned; don't create it out of nowhere.
    """
    try:
        working_dir = Path(config.vault.path).parent

        if (working_dir / ".git").exists():
            logger.debug(
                "Command refresh skipped: %s looks like a dev repo (.git present)",
                working_dir,
            )
            return

        commands_dir = working_dir / ".claude" / "commands"
        if not commands_dir.is_dir():
            logger.debug(
                "Command refresh skipped: no .claude/commands at %s", working_dir,
            )
            return

        payload = server_client.get_commands()
        claude_md = payload.get("claude_md", "")
        # Flatten each served filename to its basename and require .md — the
        # server is ours, but a write loop keyed on remote-supplied paths
        # should not be able to reach outside .claude/commands/ regardless.
        commands = {
            Path(name).name: content
            for name, content in (payload.get("commands", {}) or {}).items()
            if Path(name).name.endswith(".md")
        }

        # An empty claude_md means a server-side rendering problem, not a
        # request to blank the file she opens every session.
        if claude_md:
            (working_dir / "CLAUDE.md").write_text(claude_md, encoding="utf-8")

        # The server's set is authoritative — drop any local .md file it no
        # longer returns (a retired/renamed command).
        keep = set(commands.keys())
        for existing in commands_dir.glob("*.md"):
            if existing.name not in keep:
                existing.unlink()

        for filename, content in commands.items():
            (commands_dir / filename).write_text(content, encoding="utf-8")

        logger.info(
            "Refreshed %d commands + CLAUDE.md from server", len(commands),
        )
    except Exception:
        logger.exception("Command refresh failed — continuing without it")


async def _run_uvicorn(app, port: int, name: str):
    """Run a Starlette/ASGI app with uvicorn in an asyncio task."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
