"""Server-side vault watchdog — watches for .md file changes on disk.

Complements (does not replace) the client's `POST /api/v1/vault/push`. This
catches files arriving via Syncthing when no client is connected.

Watches the *root* of the per-user vault tree (`/vaults/`) with one observer
and derives the owner from the first path segment (`/vaults/<user>/…`). One
observer over the root beats one-per-user: users can be added without a
restart, and there is no observer to tear down when a vault goes away.
"""

import asyncio
import hashlib
import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from app.integrations.obsidian.sync import is_indexable

logger = logging.getLogger(__name__)

# Debounce: wait this many seconds after last change before re-indexing
DEBOUNCE_SECONDS = 2.0

# Limit concurrent index_single_file calls to avoid DB connection pool pressure
# during bulk syncs (e.g. rclone pushing 50 files simultaneously).
MAX_CONCURRENT_INDEX = 3


def _resolve_owner(user_name: str) -> int | None:
    """Map a vault directory name to a user id, or None if it isn't a user.

    Not cached: a miss is the interesting case (a stray directory under the
    vault root), and the lookup is a single indexed read on a two-row table.
    """
    from app.db import get_db
    from app.models.users import User

    db = get_db()
    with db.session() as session:
        row = (
            session.query(User.id)
            .filter(User.name == user_name, User.is_active.is_(True))
            .first()
        )
        return row.id if row else None


class _VaultHandler(FileSystemEventHandler):
    """Debounced handler that re-indexes changed .md files.

    `vaults_root` is the parent of the per-user vaults, so every path this
    handler sees starts with the owning user's directory name.
    """

    def __init__(self, vaults_root: Path):
        self._vaults_root = vaults_root
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._index_semaphore = threading.Semaphore(MAX_CONCURRENT_INDEX)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def _handle(self, src_path: str) -> None:
        path = Path(src_path)
        if path.suffix != ".md":
            return

        # Split /vaults/<user>/<rel> into its owner and vault-relative parts.
        try:
            from_root = path.relative_to(self._vaults_root)
        except ValueError:
            return
        if len(from_root.parts) < 2:
            return  # a file sitting directly in the root, not inside a vault
        owner_name = from_root.parts[0]
        rel = Path(*from_root.parts[1:])
        # Optimisation, not the guarantee — `index_single_file` enforces the
        # same predicate. Checking here just avoids a pointless debounce timer
        # and DB round-trip for a file that would be dropped anyway.
        if not is_indexable(rel):
            return

        rel_str = str(rel)
        # Debounce key must include the owner — both vaults can hold the same
        # relative path, and a shared key would cancel the other user's timer.
        key = f"{owner_name}/{rel_str}"

        # Debounce: cancel previous timer for this file, start a new one
        with self._lock:
            if key in self._pending:
                self._pending[key].cancel()
            timer = threading.Timer(
                DEBOUNCE_SECONDS, self._index_file, args=[path, rel_str, owner_name, key],
            )
            timer.daemon = True
            self._pending[key] = timer
            timer.start()

    def _index_file(
        self, full_path: Path, rel_path: str, owner_name: str, key: str,
    ) -> None:
        """Read file and enqueue for re-indexing under its owning user."""
        with self._lock:
            self._pending.pop(key, None)

        with self._index_semaphore:
            try:
                if not full_path.is_file():
                    return

                user_id = _resolve_owner(owner_name)
                if user_id is None:
                    logger.warning(
                        "Vault watcher: '%s' is not an active user — skipping %s",
                        owner_name, rel_path,
                    )
                    return

                content = full_path.read_text(encoding="utf-8", errors="replace")
                file_hash = hashlib.md5(full_path.read_bytes()).hexdigest()

                from app.auth.context import use_user
                from app.db import get_db
                from app.integrations.obsidian.sync import index_single_file

                db = get_db()
                with db.session() as session, use_user(user_id):
                    index_single_file(session, rel_path, content, file_hash, user_id)

                logger.info(f"Vault watcher: re-indexed {owner_name}/{rel_path}")
            except Exception:
                logger.exception(
                    f"Vault watcher: failed to index {owner_name}/{rel_path}"
                )


def start_vault_watcher(vaults_root: str) -> Observer | None:
    """Start a watchdog observer over the per-user vault tree.

    `vaults_root` is the parent directory holding `<user>/` vaults, not a
    single vault. Returns the Observer instance (call .stop() on shutdown),
    or None if the path doesn't exist.
    """
    root = Path(vaults_root)
    if not root.is_dir():
        logger.warning(f"Vault watcher: path does not exist: {vaults_root}")
        return None

    handler = _VaultHandler(root)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info(f"Vault watcher started over vault root: {vaults_root}")
    return observer


async def run_watcher_task(stop_event: asyncio.Event) -> None:
    """Startup-task entry point (manifest `background_tasks`, kind="startup").

    Supervised by `app.plugin.supervisor` — starts the watchdog observer
    (a background thread, not a coroutine) and simply waits on `stop_event`,
    tearing the observer down once it's set. If `vaults_root_path` isn't
    configured or doesn't exist, returns immediately (no-op, matching the
    pre-3.1 `if settings.vaults_root_path:` guard in main.py's lifespan).
    """
    from app.config import settings

    observer = start_vault_watcher(settings.vaults_root_path) if settings.vaults_root_path else None
    try:
        # No-op (unconfigured, or path missing) — still wait for shutdown
        # rather than returning immediately, so the supervisor doesn't spin
        # up a pointless crash-restart loop for a task with nothing to do.
        await stop_event.wait()
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
