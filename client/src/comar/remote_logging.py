"""Remote log handler — ships client logs to server via HTTP.

Buffers log records in memory and flushes them to the server periodically
or when the buffer fills. Runs the HTTP push in a background thread to
avoid blocking the logging pipeline.

Graceful degradation: if the server is unavailable, logs accumulate locally
(capped buffer, oldest dropped when full).
"""

import logging
import os
import re
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_HOME_DIR = os.path.expanduser("~")
_USER_PATH_RE = re.compile(r"/(?:Users|home)/[a-zA-Z0-9._-]+/")


def _scrub_message(message: str) -> str:
    """Replace home directory paths with ~/ to avoid leaking usernames."""
    message = message.replace(_HOME_DIR + "/", "~/")
    message = message.replace(_HOME_DIR, "~")
    return _USER_PATH_RE.sub("~/", message)

# Limits
MAX_BUFFER_SIZE = 500
FLUSH_INTERVAL = 60  # seconds
MIN_FLUSH_LEVEL = logging.INFO  # only ship INFO+ to server


class RemoteLogHandler(logging.Handler):
    """Logging handler that ships records to the comar server."""

    def __init__(self, server_client, level=MIN_FLUSH_LEVEL):
        super().__init__(level=level)
        self._client = server_client
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._start_flush_timer()

    def emit(self, record: logging.LogRecord) -> None:
        raw_msg = self.format(record) if self.formatter else record.getMessage()
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc),
            "level": record.levelname,
            "logger": record.name,
            "message": _scrub_message(raw_msg),
        }
        with self._lock:
            self._buffer.append(entry)
            # Drop oldest if buffer is full
            if len(self._buffer) > MAX_BUFFER_SIZE:
                self._buffer = self._buffer[-MAX_BUFFER_SIZE:]

            should_flush = len(self._buffer) >= MAX_BUFFER_SIZE
        if should_flush:
            self._flush_async()

    def flush(self) -> None:
        """Synchronous flush — called on shutdown."""
        self._do_flush()

    def close(self) -> None:
        self._cancel_timer()
        self._do_flush()
        super().close()

    def _start_flush_timer(self) -> None:
        self._timer = threading.Timer(FLUSH_INTERVAL, self._timer_flush)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _timer_flush(self) -> None:
        """Called by the timer thread — flush and restart timer."""
        self._do_flush()
        self._start_flush_timer()

    def _flush_async(self) -> None:
        """Flush in a background thread to avoid blocking the caller."""
        t = threading.Thread(target=self._do_flush, daemon=True)
        t.start()

    def _do_flush(self) -> None:
        """Actually send buffered entries to the server."""
        with self._lock:
            if not self._buffer:
                return
            entries = self._buffer.copy()
            self._buffer.clear()

        if not self._client.is_connected:
            # Put entries back — server is down
            with self._lock:
                self._buffer = entries + self._buffer
                if len(self._buffer) > MAX_BUFFER_SIZE:
                    self._buffer = self._buffer[-MAX_BUFFER_SIZE:]
            return

        try:
            self._client.push_logs(entries)
        except Exception:
            # Put entries back on failure (silently — avoid recursive logging)
            with self._lock:
                self._buffer = entries + self._buffer
                if len(self._buffer) > MAX_BUFFER_SIZE:
                    self._buffer = self._buffer[-MAX_BUFFER_SIZE:]
