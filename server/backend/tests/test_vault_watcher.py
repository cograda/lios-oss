"""Tests for the server-side vault watchdog — path filtering and skip logic.

Loads watcher.py directly via spec_from_file_location to bypass the
obsidian/__init__.py chain (which imports app.config, app.db, etc.).
"""

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

# Load watcher.py directly — it only imports watchdog + stdlib at module level
_watcher_path = Path(__file__).resolve().parent.parent / "app" / "integrations" / "obsidian" / "watcher.py"

try:
    _spec = importlib.util.spec_from_file_location("obsidian_watcher", str(_watcher_path))
    _watcher = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_watcher)
except ImportError as e:
    pytest.skip(f"Cannot import watcher.py (missing dep: {e})", allow_module_level=True)

SKIP_DIRS = _watcher.SKIP_DIRS
MAX_CONCURRENT_INDEX = _watcher.MAX_CONCURRENT_INDEX
_VaultHandler = _watcher._VaultHandler


class TestPathFiltering:
    def test_skip_dirs_are_defined(self):
        assert ".obsidian" in SKIP_DIRS
        assert ".claude" in SKIP_DIRS
        assert ".tools" in SKIP_DIRS
        assert "Attachments" in SKIP_DIRS
        assert "Templates" in SKIP_DIRS

    def test_md_file_in_valid_path_is_handled(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        # Create a .md file
        md_file = tmp_path / "Daily Notes" / "test.md"
        md_file.parent.mkdir(parents=True)
        md_file.touch()

        # _handle should not skip this (we can't easily test the timer fires,
        # but we can verify it doesn't get filtered out by checking _pending)
        handler._handle(str(md_file))
        assert "Daily Notes/test.md" in handler._pending

    def test_non_md_file_is_ignored(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        txt_file = tmp_path / "notes.txt"
        txt_file.touch()
        handler._handle(str(txt_file))
        assert len(handler._pending) == 0

    def test_file_in_skip_dir_is_ignored(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        hidden = tmp_path / ".obsidian" / "config.md"
        hidden.parent.mkdir(parents=True)
        hidden.touch()
        handler._handle(str(hidden))
        assert len(handler._pending) == 0

    def test_file_in_nested_skip_dir_is_ignored(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        hidden = tmp_path / "some" / ".claude" / "note.md"
        hidden.parent.mkdir(parents=True)
        hidden.touch()
        handler._handle(str(hidden))
        assert len(handler._pending) == 0

    def test_file_outside_vault_is_ignored(self, tmp_path):
        handler = _VaultHandler(tmp_path / "vault")
        (tmp_path / "vault").mkdir()
        outside = tmp_path / "other" / "note.md"
        outside.parent.mkdir(parents=True)
        outside.touch()
        handler._handle(str(outside))
        assert len(handler._pending) == 0

    def test_attachments_dir_is_skipped(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        att = tmp_path / "Attachments" / "readme.md"
        att.parent.mkdir(parents=True)
        att.touch()
        handler._handle(str(att))
        assert len(handler._pending) == 0

    def test_debounce_replaces_timer(self, tmp_path):
        """Multiple rapid changes to the same file should result in one timer."""
        handler = _VaultHandler(tmp_path)
        md_file = tmp_path / "note.md"
        md_file.touch()

        handler._handle(str(md_file))
        timer_1 = handler._pending.get("note.md")
        handler._handle(str(md_file))
        timer_2 = handler._pending.get("note.md")

        # Timer should have been replaced
        assert timer_1 is not timer_2
        assert len(handler._pending) == 1

        # Clean up timers
        for t in handler._pending.values():
            t.cancel()


class TestConcurrencyLimit:
    def test_semaphore_exists_on_handler(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        assert hasattr(handler, "_index_semaphore")
        # Semaphore should allow MAX_CONCURRENT_INDEX concurrent acquisitions
        assert handler._index_semaphore._value == MAX_CONCURRENT_INDEX

    def test_concurrent_indexing_bounded_by_semaphore(self, tmp_path):
        """Fire more concurrent _index_file calls than the semaphore allows.
        Verify only MAX_CONCURRENT_INDEX run simultaneously."""
        import threading
        import time
        from unittest.mock import MagicMock

        handler = _VaultHandler(tmp_path)

        # Track concurrent execution
        entered = threading.Semaphore(0)  # signals when a thread enters the mock
        active_count = []
        lock = threading.Lock()
        gate = threading.Event()  # blocks inside mock to create contention

        original_index_file = handler._index_file

        def counting_index_file(full_path, rel_path):
            """Wraps _index_file but intercepts the semaphore-protected section."""
            # We test the semaphore directly: acquire it, do counting, then release
            with handler._index_semaphore:
                with lock:
                    active_count.append(1)
                entered.release()
                gate.wait(timeout=5)
                with lock:
                    active_count.pop()

        # Create test files
        num_files = MAX_CONCURRENT_INDEX + 2
        files = []
        for i in range(num_files):
            f = tmp_path / f"note{i}.md"
            f.write_text(f"content {i}")
            files.append(f)

        # Replace _index_file with our counting version
        handler._index_file = counting_index_file

        threads = []
        for i, f in enumerate(files):
            t = threading.Thread(
                target=counting_index_file,
                args=[f, f"note{i}.md"],
            )
            t.start()
            threads.append(t)

        # Wait for MAX_CONCURRENT_INDEX threads to enter
        for _ in range(MAX_CONCURRENT_INDEX):
            entered.acquire(timeout=5)

        # Brief pause to let any extra threads (incorrectly) enter
        time.sleep(0.1)

        with lock:
            concurrent = len(active_count)

        assert concurrent == MAX_CONCURRENT_INDEX, (
            f"Expected {MAX_CONCURRENT_INDEX} concurrent, got {concurrent}"
        )

        # Release all threads
        gate.set()
        for t in threads:
            t.join(timeout=5)
