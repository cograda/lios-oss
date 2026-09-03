"""Voice Memos watcher — notices new recordings and uploads them. Nothing more.

This is the *only* part of voice-memo capture that has to happen on the Mac,
because the recordings live in a container only a local process with Full Disk
Access can read:

    ~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/

Everything else — classifying the file, reading its duration, transcribing it,
building the proper-noun dictionary, routing the result — happens on the server.
The daemon deliberately holds no OpenAI key, parses no atoms and makes no
decisions about content. That keeps it what the architecture says it is: a
side-car for jobs that physically must be local (EventKit, fsevents), matching
`vault_watcher.py`'s scope.

Two things are genuinely this module's problem:

  - **Recordings are written progressively.** A new memo appears the moment
    recording starts, and grows until it stops. Uploading on the first event
    would ship a truncated file, so a memo is only sent once its size has been
    stable for `SETTLE_SECONDS`.
  - **Uploading twice costs money.** The server pays per transcription, so a
    daemon restart, a Time Machine restore or an iCloud re-sync touching mtimes
    must not re-upload. Hence the on-disk ledger keyed by content identity
    rather than by path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from lios_sync.server_client import ServerClient

logger = logging.getLogger(__name__)

RECORDINGS_DIR = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.com.apple.VoiceMemos.shared"
    / "Recordings"
)

# `.qta` is what memos synced from another device (or with a studio mix) land as
# — a byte-compatible MP4 with a different extension. Both are audio.
AUDIO_SUFFIXES = {".m4a", ".qta"}

# A recording is considered finished once its size hasn't changed for this long.
SETTLE_SECONDS = 20.0

# Below this, a memo is a mis-tap rather than a note. Matches the threshold the
# sandbox triage arrived at over 373 real memos (31 came in under it).
# Enforced by *size* here, not duration — reading duration would mean parsing
# the container, which is the server's job. ~8 KB/s at Voice Memos' default
# bitrate, so this is a deliberately loose floor.
MIN_BYTES = 24 * 1024

# Cap a single upload. The server rejects oversized files anyway; no point
# spending minutes of Wi-Fi to find that out.
MAX_BYTES = 25 * 1024 * 1024

LEDGER_PATH = Path.home() / ".config" / "lios" / "voice_memos_uploaded.json"

# Bound the ledger so it can't grow forever. Well above the ~373 memos this
# vault accumulated over two years.
MAX_LEDGER_ENTRIES = 5000


def _fingerprint(path: Path) -> str:
    """Content-derived id for a recording.

    Deliberately not the path: Voice Memos renames files when a memo is retitled,
    and a restore can change mtimes wholesale. Hashing the first and last 64 KB
    plus the size identifies the *recording*, cheaply, without reading megabytes.
    Two distinct memos colliding would need identical size and identical head and
    tail — not a realistic risk for audio.
    """
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(65536))
        if size > 131072:
            handle.seek(-65536, 2)
            digest.update(handle.read(65536))
    return digest.hexdigest()[:32]


class UploadLedger:
    """Records which recordings have been uploaded, so none is sent twice.

    Persisted as JSON next to the daemon's config. Written after every upload
    rather than at shutdown — a daemon that's killed must not forget what it
    already paid for.
    """

    def __init__(self, path: Path = LEDGER_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._entries: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            # A corrupt ledger would re-upload everything, which costs money —
            # so this is loud, and recovery is manual rather than automatic.
            logger.exception(
                "Voice-memo ledger at %s is unreadable — refusing to upload "
                "anything until it's fixed or deleted", self.path,
            )
            raise

    def __contains__(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._entries

    def add(self, fingerprint: str, name: str) -> None:
        with self._lock:
            self._entries[fingerprint] = f"{name}@{datetime.now(timezone.utc).isoformat()}"
            if len(self._entries) > MAX_LEDGER_ENTRIES:
                # Drop oldest by insertion order (dicts preserve it).
                for key in list(self._entries)[: len(self._entries) - MAX_LEDGER_ENTRIES]:
                    del self._entries[key]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")
            except OSError:
                logger.exception("Could not persist voice-memo ledger to %s", self.path)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)


class VoiceMemoHandler(FileSystemEventHandler):
    """Debounces filesystem events until a recording has finished being written."""

    def __init__(self, server_client: ServerClient, ledger: UploadLedger):
        self.server_client = server_client
        self.ledger = ledger
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self.uploaded = 0
        self.failed = 0

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent):
            self._schedule(event.src_path)

    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent):
            self._schedule(event.src_path)

    def _schedule(self, path_str: str) -> None:
        path = Path(path_str)
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            return

        key = str(path)
        with self._lock:
            existing = self._timers.get(key)
            if existing:
                existing.cancel()
            timer = threading.Timer(SETTLE_SECONDS, self._settle, args=[path])
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _settle(self, path: Path) -> None:
        """Fired once no event has touched `path` for SETTLE_SECONDS."""
        with self._lock:
            self._timers.pop(str(path), None)
        try:
            self.upload(path)
        except Exception:
            logger.exception("Voice-memo upload failed for %s", path.name)

    def upload(self, path: Path) -> bool:
        """Upload one recording if it's eligible and not already sent."""
        if not path.is_file():
            return False

        size = path.stat().st_size
        if size < MIN_BYTES:
            logger.debug("Skipping %s (%d bytes — below the mis-tap floor)", path.name, size)
            return False
        if size > MAX_BYTES:
            logger.info(
                "Skipping %s (%.1f MB — over the server's upload cap)",
                path.name, size / (1024 * 1024),
            )
            return False

        fingerprint = _fingerprint(path)
        if fingerprint in self.ledger:
            logger.debug("Skipping %s — already uploaded", path.name)
            return False

        data = path.read_bytes()
        # Re-check after reading: if the file grew between settle and read, it was
        # still being written and the fingerprint no longer describes what we
        # hold. Let the next event reschedule it.
        if len(data) != size:
            logger.debug("%s changed while reading — deferring", path.name)
            return False

        try:
            self.server_client.ingest_inbox_file(
                path.name,
                data,
                file_type="audio",
                metadata={
                    "source": "voice-memo",
                    # mtime is the closest thing to "when it was recorded" that
                    # doesn't require parsing the container.
                    "recorded_at": datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    ).isoformat(),
                },
            )
        except Exception:
            self.failed += 1
            # NOT added to the ledger: a failed upload must be retried, and the
            # server never charged for it.
            raise

        # Ledger only after the server confirms, so a network failure can't cause
        # a permanently-skipped memo.
        self.ledger.add(fingerprint, path.name)
        self.uploaded += 1
        logger.info("Uploaded voice memo %s (%.1f KB)", path.name, size / 1024)
        return True

    def scan_existing(self, limit: int | None = None) -> dict[str, int]:
        """Upload recordings already on disk — the backfill path.

        Not run automatically at startup: on a fresh install that would upload
        every memo ever recorded, and the server pays per transcription. Driven
        explicitly by `lios-sync voice-memos backfill` instead.
        """
        counts = {"considered": 0, "uploaded": 0, "skipped": 0}
        if not RECORDINGS_DIR.is_dir():
            return counts

        for path in sorted(RECORDINGS_DIR.iterdir()):
            if path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            counts["considered"] += 1
            if limit is not None and counts["uploaded"] >= limit:
                break
            try:
                if self.upload(path):
                    counts["uploaded"] += 1
                else:
                    counts["skipped"] += 1
            except Exception:
                logger.exception("Backfill upload failed for %s", path.name)
        return counts


def start_voice_memo_watcher(
    server_client: ServerClient,
    recordings_dir: Path = RECORDINGS_DIR,
) -> tuple[Observer, VoiceMemoHandler] | None:
    """Start watching for new voice memos.

    Returns None (having logged why) if the container isn't readable — normally
    because the process lacks Full Disk Access, which is a setup problem the user
    has to fix in System Settings, not something to retry.
    """
    if not recordings_dir.is_dir():
        logger.info("Voice Memos container not found at %s — watcher not started", recordings_dir)
        return None
    try:
        next(recordings_dir.iterdir(), None)
    except PermissionError:
        logger.warning(
            "No permission to read %s — grant Full Disk Access to the daemon "
            "in System Settings › Privacy & Security to enable voice-memo capture",
            recordings_dir,
        )
        return None

    handler = VoiceMemoHandler(server_client, UploadLedger())
    observer = Observer()
    # Non-recursive: recordings are flat in this directory, and the container has
    # sibling metadata (CloudRecordings.db and its WAL) that changes constantly.
    observer.schedule(handler, str(recordings_dir), recursive=False)
    observer.start()
    logger.info(
        "Voice-memo watcher started: %s (%d already uploaded)",
        recordings_dir, handler.ledger.count,
    )
    return observer, handler
