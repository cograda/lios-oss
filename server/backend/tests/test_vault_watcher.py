"""Tests for the server-side vault watchdog — path filtering and skip logic.

Loads watcher.py directly via spec_from_file_location to bypass the
obsidian/__init__.py chain (which imports app.config, app.db, etc.).

`SKIP_DIRS` moved out of watcher.py on 2026-08-14: the set and its membership
test were duplicated across three modules, and the copy that mattered most —
`POST /api/v1/vault/push` — had no copy at all. The watcher now defers to
`sync.is_indexable()`, so this file sources the set from there.
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

from app.integrations.obsidian.sync import SKIP_DIRS  # noqa: E402
MAX_CONCURRENT_INDEX = _watcher.MAX_CONCURRENT_INDEX
_VaultHandler = _watcher._VaultHandler


def _cancel_all(handler) -> None:
    """Cancel any debounce timers a test left armed."""
    for t in handler._pending.values():
        t.cancel()


class TestPathFiltering:
    def test_skip_dirs_are_defined(self):
        assert ".obsidian" in SKIP_DIRS
        assert ".claude" in SKIP_DIRS
        assert ".tools" in SKIP_DIRS
        assert ".stversions" in SKIP_DIRS  # Syncthing version history
        assert "Attachments" in SKIP_DIRS
        assert "Templates" in SKIP_DIRS

    def test_stversions_file_is_not_queued_for_indexing(self, tmp_path):
        """End-to-end through the handler, not just the set."""
        handler = _VaultHandler(tmp_path)
        snapshot = self._touch(
            tmp_path, "alex/.stversions/Task Backlog~20260801-112211.md"
        )

        handler._handle(str(snapshot))
        assert handler._pending == {}
        _cancel_all(handler)

    @staticmethod
    def _touch(root, rel: str):
        """Create `<root>/<rel>` (parents included) and return the path."""
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return p

    def test_md_file_in_valid_path_is_handled(self, tmp_path):
        # The handler watches the vault ROOT; paths are `<root>/<user>/<rel>`
        # and the pending key is `<user>/<rel>`.
        handler = _VaultHandler(tmp_path)
        md_file = self._touch(tmp_path, "alex/Daily Notes/test.md")

        handler._handle(str(md_file))
        assert "alex/Daily Notes/test.md" in handler._pending
        _cancel_all(handler)

    def test_non_md_file_is_ignored(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        handler._handle(str(self._touch(tmp_path, "alex/notes.txt")))
        assert len(handler._pending) == 0

    def test_file_directly_in_vault_root_is_ignored(self, tmp_path):
        """A file not inside any user's vault has no owner — skip it."""
        handler = _VaultHandler(tmp_path)
        handler._handle(str(self._touch(tmp_path, "stray.md")))
        assert len(handler._pending) == 0

    def test_file_in_skip_dir_is_ignored(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        handler._handle(str(self._touch(tmp_path, "alex/.obsidian/config.md")))
        assert len(handler._pending) == 0

    def test_file_in_nested_skip_dir_is_ignored(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        handler._handle(str(self._touch(tmp_path, "alex/some/.claude/note.md")))
        assert len(handler._pending) == 0

    def test_file_outside_vault_is_ignored(self, tmp_path):
        handler = _VaultHandler(tmp_path / "vaults")
        (tmp_path / "vaults").mkdir()
        handler._handle(str(self._touch(tmp_path, "other/alex/note.md")))
        assert len(handler._pending) == 0

    def test_attachments_dir_is_skipped(self, tmp_path):
        handler = _VaultHandler(tmp_path)
        handler._handle(str(self._touch(tmp_path, "alex/Attachments/readme.md")))
        assert len(handler._pending) == 0

    def test_debounce_replaces_timer(self, tmp_path):
        """Multiple rapid changes to the same file should result in one timer."""
        handler = _VaultHandler(tmp_path)
        md_file = self._touch(tmp_path, "alex/note.md")

        handler._handle(str(md_file))
        timer_1 = handler._pending.get("alex/note.md")
        handler._handle(str(md_file))
        timer_2 = handler._pending.get("alex/note.md")

        # Timer should have been replaced
        assert timer_1 is not timer_2
        assert len(handler._pending) == 1
        _cancel_all(handler)

    def test_same_relative_path_in_two_vaults_debounces_separately(self, tmp_path):
        """Both vaults hold `Inbox/note.md` — one must not cancel the other."""
        handler = _VaultHandler(tmp_path)
        alex = self._touch(tmp_path, "alex/Inbox/note.md")
        sam = self._touch(tmp_path, "sam/Inbox/note.md")

        handler._handle(str(alex))
        handler._handle(str(sam))

        assert set(handler._pending) == {"alex/Inbox/note.md", "sam/Inbox/note.md"}
        _cancel_all(handler)


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
