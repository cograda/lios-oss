"""Tests for the remote log handler — buffer management, flush, degradation."""

import logging
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from comar.remote_logging import RemoteLogHandler, MAX_BUFFER_SIZE, _scrub_message


def _make_handler(connected=True):
    """Create a RemoteLogHandler with a mock gRPC client."""
    client = MagicMock()
    client.is_connected = connected
    handler = RemoteLogHandler(client)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler, client


def _make_record(msg="test message", level=logging.INFO):
    """Create a log record."""
    record = logging.LogRecord(
        name="test", level=level, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )
    return record


class TestBuffering:
    def test_emit_adds_to_buffer(self):
        handler, _ = _make_handler()
        handler.emit(_make_record("hello"))
        assert len(handler._buffer) == 1
        assert handler._buffer[0]["message"] == "hello"

    def test_buffer_caps_at_max_size(self):
        handler, _ = _make_handler(connected=False)
        for i in range(MAX_BUFFER_SIZE + 50):
            handler.emit(_make_record(f"msg {i}"))
        assert len(handler._buffer) <= MAX_BUFFER_SIZE

    def test_oldest_dropped_when_buffer_full(self):
        handler, _ = _make_handler(connected=False)
        for i in range(MAX_BUFFER_SIZE + 10):
            handler.emit(_make_record(f"msg {i}"))
        # Oldest messages should have been dropped
        messages = [e["message"] for e in handler._buffer]
        assert "msg 0" not in messages
        assert f"msg {MAX_BUFFER_SIZE + 9}" in messages

    def test_entry_has_correct_fields(self):
        handler, _ = _make_handler()
        handler.emit(_make_record("test"))
        entry = handler._buffer[0]
        assert "timestamp" in entry
        assert entry["level"] == "INFO"
        assert entry["logger"] == "test"
        assert isinstance(entry["timestamp"], datetime)


class TestFlush:
    def test_flush_sends_to_server(self):
        handler, client = _make_handler(connected=True)
        handler.emit(_make_record("hello"))
        handler._do_flush()
        client.push_logs.assert_called_once()
        entries = client.push_logs.call_args[0][0]
        assert len(entries) == 1
        assert entries[0]["message"] == "hello"

    def test_flush_clears_buffer(self):
        handler, client = _make_handler(connected=True)
        handler.emit(_make_record("hello"))
        handler._do_flush()
        assert len(handler._buffer) == 0

    def test_flush_when_disconnected_preserves_buffer(self):
        handler, client = _make_handler(connected=False)
        handler.emit(_make_record("hello"))
        handler._do_flush()
        client.push_logs.assert_not_called()
        assert len(handler._buffer) == 1

    def test_flush_on_error_preserves_buffer(self):
        handler, client = _make_handler(connected=True)
        client.push_logs.side_effect = Exception("gRPC error")
        handler.emit(_make_record("hello"))
        handler._do_flush()
        assert len(handler._buffer) == 1

    def test_empty_flush_is_noop(self):
        handler, client = _make_handler()
        handler._do_flush()
        client.push_logs.assert_not_called()

    def test_close_flushes(self):
        handler, client = _make_handler(connected=True)
        handler.emit(_make_record("goodbye"))
        handler.close()
        client.push_logs.assert_called_once()


class TestPathScrubbing:
    def test_scrubs_home_dir(self):
        import os
        home = os.path.expanduser("~")
        assert _scrub_message(f"Error in {home}/foo/bar.py") == "Error in ~/foo/bar.py"

    def test_scrubs_home_dir_without_trailing_slash(self):
        import os
        home = os.path.expanduser("~")
        assert _scrub_message(f"Path is {home}") == "Path is ~"

    def test_scrubs_other_user_paths(self):
        result = _scrub_message("File at /Users/sam/Documents/file.md")
        assert result == "File at ~/Documents/file.md"

    def test_scrubs_linux_paths(self):
        result = _scrub_message("Error in /home/ubuntu/app/main.py")
        assert result == "Error in ~/app/main.py"

    def test_preserves_non_path_content(self):
        msg = "Error: connection refused on port 9400"
        assert _scrub_message(msg) == msg

    def test_scrubs_multiple_paths(self):
        result = _scrub_message(
            "Copied /Users/alex/a.txt to /Users/sam/b.txt"
        )
        assert "/Users/alex/" not in result
        assert "/Users/sam/" not in result
        assert "~/a.txt" in result
        assert "~/b.txt" in result

    def test_emit_scrubs_paths(self):
        """Integration: verify emit() applies scrubbing to buffered entries."""
        import os
        handler, _ = _make_handler()
        home = os.path.expanduser("~")
        handler.emit(_make_record(f"Failed to read {home}/vault/note.md"))
        assert handler._buffer[0]["message"] == "Failed to read ~/vault/note.md"


class TestLevelFiltering:
    def test_debug_records_below_threshold_are_dropped(self):
        handler, _ = _make_handler()
        # Handler default level is INFO, so DEBUG should be filtered
        record = _make_record("debug msg", level=logging.DEBUG)
        # The logging framework filters by level before calling emit,
        # but we can test the handler's level attribute
        assert handler.level >= logging.INFO
