"""Server-side vault watchdog — watches for .md file changes on disk.

Complements (does not replace) the client's gRPC PushVaultFile. This catches
files arriving via Google Drive / rclone sync when no client is connected.
Uses watchdog to monitor the vault directory inside the Docker container.
"""

import hashlib
import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# Directories to skip (same as client vault watcher and sync.py)
SKIP_DIRS = {".obsidian", ".claude", ".tools", ".embeddings", ".trash", ".git", "Attachments", "Templates"}

# Debounce: wait this many seconds after last change before re-indexing
DEBOUNCE_SECONDS = 2.0

# Limit concurrent index_single_file calls to avoid DB connection pool pressure
# during bulk syncs (e.g. rclone pushing 50 files simultaneously).
MAX_CONCURRENT_INDEX = 3


class _VaultHandler(FileSystemEventHandler):
    """Debounced handler that re-indexes changed .md files."""

    def __init__(self, vault_path: Path):
        self._vault_path = vault_path
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

        # Check skip dirs
        try:
            rel = path.relative_to(self._vault_path)
        except ValueError:
            return
        if any(part in SKIP_DIRS for part in rel.parts):
            return

        rel_str = str(rel)

        # Debounce: cancel previous timer for this file, start a new one
        with self._lock:
            if rel_str in self._pending:
                self._pending[rel_str].cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, self._index_file, args=[path, rel_str])
            timer.daemon = True
            self._pending[rel_str] = timer
            timer.start()

    def _index_file(self, full_path: Path, rel_path: str) -> None:
        """Read file and enqueue for re-indexing."""
        with self._lock:
            self._pending.pop(rel_path, None)

        with self._index_semaphore:
            try:
                if not full_path.is_file():
                    return

                content = full_path.read_text(encoding="utf-8", errors="replace")
                file_hash = hashlib.md5(full_path.read_bytes()).hexdigest()

                from app.db import get_db
                from app.integrations.obsidian.sync import index_single_file

                db = get_db()
                with db.session() as session:
                    index_single_file(session, rel_path, content, file_hash)

                logger.info(f"Vault watcher: re-indexed {rel_path}")
            except Exception:
                logger.exception(f"Vault watcher: failed to index {rel_path}")


def start_vault_watcher(vault_path: str) -> Observer | None:
    """Start watchdog observer on the vault directory.

    Returns the Observer instance (call .stop() on shutdown), or None
    if the vault path doesn't exist.
    """
    vp = Path(vault_path)
    if not vp.is_dir():
        logger.warning(f"Vault watcher: path does not exist: {vault_path}")
        return None

    handler = _VaultHandler(vp)
    observer = Observer()
    observer.schedule(handler, str(vp), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info(f"Vault watcher started: {vault_path}")
    return observer
