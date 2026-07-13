"""Vault watcher — monitors vault directory via fsevents, pushes changes to server.

NOTE ON ARCHITECTURE: This is the "fast path" watcher. It ensures changes made on
the active Mac are indexed by the server in seconds, enabling the core workflow:
"jot a note, immediately ask Claude about it". The server has its own watchdog
acting as a backstop for indirect changes (e.g., Google Drive syncs from other
devices) which have minutes of latency.

Uses watchdog to watch for .md file changes in the vault directory.
On change, reads the file, computes MD5 hash, and POSTs to /api/v1/vault/push.
Failed pushes are queued for retry on next successful push.
"""

import hashlib
import logging
import threading
import time
from collections import deque
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
from watchdog.observers import Observer

from comar.server_client import ServerClient

logger = logging.getLogger(__name__)

# Directories to ignore (same as server-side indexing)
SKIP_DIRS = {".obsidian", ".claude", ".tools", ".embeddings", ".trash", ".git", "Attachments", "Templates"}

# Max queued items for retry (prevents unbounded memory use)
MAX_RETRY_QUEUE = 100

# How often to retry queued pushes while the queue is non-empty. Without
# this, queued items sat until the *next* vault edit triggered a push —
# potentially hours of avoidable staleness after a brief network blip.
RETRY_DRAIN_INTERVAL = 60.0


class VaultHandler(FileSystemEventHandler):
    """Handles vault file changes and pushes to server."""

    # Files modified by external processes in the last N seconds are "recently modified"
    RECENT_WINDOW = 30

    def __init__(self, vault_path: Path, server_client: ServerClient):
        self.vault_path = vault_path
        self.server_client = server_client
        self._debounce: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._retry_queue: deque[tuple[str, str, str]] = deque(maxlen=MAX_RETRY_QUEUE)
        self._drain_timer: threading.Timer | None = None
        # Track recently-modified files: rel_path → timestamp
        self._recent_mods: dict[str, float] = {}

    @property
    def retry_queue_depth(self) -> int:
        return len(self._retry_queue)

    def recently_modified(self) -> dict[str, float]:
        """Return files modified by external processes within the recent window.

        Returns dict of rel_path → seconds_ago.
        """
        now = time.time()
        cutoff = now - self.RECENT_WINDOW
        # Prune old entries
        self._recent_mods = {k: v for k, v in self._recent_mods.items() if v > cutoff}
        return {k: round(now - v, 1) for k, v in self._recent_mods.items()}

    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent):
            self._handle(event.src_path)

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent):
            self._handle(event.src_path)

    def _handle(self, path_str: str):
        path = Path(path_str)

        # Only handle .md files
        if path.suffix != ".md":
            return

        # Skip ignored directories
        try:
            rel_parts = path.relative_to(self.vault_path).parts
        except ValueError:
            return
        if any(part in SKIP_DIRS for part in rel_parts):
            return

        rel_path = str(path.relative_to(self.vault_path))

        # Track external modification time for active-file detection
        self._recent_mods[rel_path] = time.time()

        # Debounce: wait 1 second after last change before pushing
        # (editors often trigger multiple events for one save)
        with self._lock:
            if rel_path in self._debounce:
                self._debounce[rel_path].cancel()
            timer = threading.Timer(1.0, self._push, args=[path, rel_path])
            self._debounce[rel_path] = timer
            timer.start()

    def _push(self, full_path: Path, rel_path: str):
        """Read the file and push to server. Queue for retry on failure."""
        try:
            if not full_path.is_file():
                return

            content = full_path.read_text(encoding="utf-8", errors="replace")
            file_hash = hashlib.md5(content.encode("utf-8")).hexdigest()

            self.server_client.push_vault_file(rel_path, content, file_hash)
            logger.debug(f"Pushed {rel_path}")

            # Success — drain retry queue
            self._drain_retry_queue()

        except Exception:
            logger.warning(f"Failed to push {rel_path} — queued for retry")
            # Re-read content for queue (file may change before retry)
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
                file_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
                # Deduplicate: remove older entry for same path. Keep the
                # maxlen bound — a plain deque() here would grow unbounded.
                self._retry_queue = deque(
                    ((p, c, h) for p, c, h in self._retry_queue if p != rel_path),
                    maxlen=MAX_RETRY_QUEUE,
                )
                self._retry_queue.append((rel_path, content, file_hash))
                logger.info(f"Retry queue depth: {len(self._retry_queue)}")
                self._schedule_drain()
            except Exception:
                logger.exception(f"Failed to queue {rel_path} for retry")
        finally:
            with self._lock:
                self._debounce.pop(rel_path, None)

    def _drain_retry_queue(self):
        """Push all queued items after a successful push."""
        drained = 0
        while self._retry_queue:
            rel_path, content, file_hash = self._retry_queue[0]
            try:
                self.server_client.push_vault_file(rel_path, content, file_hash)
                self._retry_queue.popleft()
                drained += 1
            except Exception:
                # Server still having issues — stop draining
                break
        if drained:
            logger.info(f"Drained {drained} items from retry queue")

    def _schedule_drain(self):
        """Arm the periodic drain timer while the retry queue is non-empty."""
        with self._lock:
            if self._drain_timer is not None or not self._retry_queue:
                return
            timer = threading.Timer(RETRY_DRAIN_INTERVAL, self._drain_tick)
            timer.daemon = True
            self._drain_timer = timer
            timer.start()

    def _drain_tick(self):
        with self._lock:
            self._drain_timer = None
        self._drain_retry_queue()
        self._schedule_drain()  # re-arm if items remain


def start_vault_watcher(vault_path: Path, server_client: ServerClient) -> tuple[Observer, VaultHandler]:
    """Start watching the vault directory for changes.

    Returns (observer, handler) so the handler's retry_queue_depth
    can be exposed via the health endpoint.
    """
    handler = VaultHandler(vault_path, server_client)
    observer = Observer()
    observer.schedule(handler, str(vault_path), recursive=True)
    observer.start()
    logger.info(f"Vault watcher started: {vault_path}")
    return observer, handler
