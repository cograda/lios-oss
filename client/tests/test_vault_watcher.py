"""Client vault watcher — retry queue and periodic drain."""

from collections import deque
from unittest.mock import MagicMock

from lios_sync.vault_watcher import MAX_RETRY_QUEUE, VaultHandler


def _handler(tmp_path, push_side_effect=None):
    client = MagicMock()
    if push_side_effect is not None:
        client.push_vault_file.side_effect = push_side_effect
    return VaultHandler(tmp_path, client), client


def _write(tmp_path, name, content="hello"):
    p = tmp_path / name
    p.write_text(content)
    return p


def test_failed_push_queues_and_arms_drain_timer(tmp_path):
    handler, client = _handler(tmp_path, push_side_effect=RuntimeError("down"))
    note = _write(tmp_path, "Note.md")

    handler._push(note, "Note.md")

    assert handler.retry_queue_depth == 1
    assert handler._drain_timer is not None
    handler._drain_timer.cancel()


def test_drain_tick_pushes_queued_items_when_server_recovers(tmp_path):
    handler, client = _handler(tmp_path)
    handler._retry_queue.append(("Note.md", "hello", "abc"))

    handler._drain_tick()

    client.push_vault_file.assert_called_once_with("Note.md", "hello", "abc")
    assert handler.retry_queue_depth == 0
    assert handler._drain_timer is None  # nothing left — not re-armed


def test_drain_tick_rearms_while_server_still_down(tmp_path):
    handler, client = _handler(tmp_path, push_side_effect=RuntimeError("down"))
    handler._retry_queue.append(("Note.md", "hello", "abc"))

    handler._drain_tick()

    assert handler.retry_queue_depth == 1
    assert handler._drain_timer is not None  # re-armed for next attempt
    handler._drain_timer.cancel()


def test_retry_dedup_preserves_maxlen_bound(tmp_path):
    handler, client = _handler(tmp_path, push_side_effect=RuntimeError("down"))
    note = _write(tmp_path, "Note.md")

    # Fail the same path twice — dedup rebuilds the deque; the rebuilt
    # deque must keep its bound (this regressed once: deque() without
    # maxlen made the queue unbounded after the first rebuild).
    handler._push(note, "Note.md")
    handler._push(note, "Note.md")

    assert handler.retry_queue_depth == 1
    assert handler._retry_queue.maxlen == MAX_RETRY_QUEUE
    handler._drain_timer.cancel()


def test_successful_push_drains_queue(tmp_path):
    handler, client = _handler(tmp_path)
    handler._retry_queue.append(("Old.md", "stale", "h1"))
    note = _write(tmp_path, "New.md")

    handler._push(note, "New.md")

    pushed_paths = [c[0][0] for c in client.push_vault_file.call_args_list]
    assert pushed_paths == ["New.md", "Old.md"]
    assert handler.retry_queue_depth == 0


def test_non_md_and_skip_dirs_ignored(tmp_path):
    handler, client = _handler(tmp_path)

    handler._handle(str(tmp_path / "image.png"))
    handler._handle(str(tmp_path / ".obsidian" / "config.md"))
    handler._handle(str(tmp_path / "Templates" / "T.md"))

    assert not handler._debounce  # no push scheduled for any of them


class TestEchoLoopGuard:
    """No-op pushes must not happen — they were an infinite loop.

    The vault has two transports for the same files: Syncthing replicates it
    laptop<->server, and `POST /api/v1/vault/push` writes what we send into
    that same tree. Rewriting identical bytes still bumps mtime, and watchdog
    fires on metadata changes, so the round trip fed itself:

        push X -> server rewrites X -> Syncthing returns the mtime
          -> fsevents -> push X -> ...

    Measured live 2026-08-14 at roughly one lap per 8-10s across 17 files,
    running since at least 09 Aug. It cost no embeddings (the indexer dedups on
    file_hash) but it generated Syncthing conflict files and made every mtime
    in the working set meaningless, which is exactly what `vault_recent` reads.
    """

    def test_identical_content_is_not_pushed_twice(self, tmp_path):
        handler, client = _handler(tmp_path)
        note = _write(tmp_path, "Note.md", "same bytes")

        handler._push(note, "Note.md")
        handler._push(note, "Note.md")  # the echo

        assert client.push_vault_file.call_count == 1

    def test_a_real_edit_still_pushes(self, tmp_path):
        """The guard must not swallow genuine changes."""
        handler, client = _handler(tmp_path)
        note = _write(tmp_path, "Note.md", "first")
        handler._push(note, "Note.md")

        note.write_text("second")
        handler._push(note, "Note.md")

        assert client.push_vault_file.call_count == 2

    def test_reverting_to_earlier_content_still_pushes(self, tmp_path):
        """Only the *last* push is remembered, so A->B->A is three pushes.

        Keying on a set of every hash ever seen would silently drop the revert
        and leave the server holding B forever.
        """
        handler, client = _handler(tmp_path)
        note = _write(tmp_path, "Note.md", "A")
        handler._push(note, "Note.md")
        note.write_text("B")
        handler._push(note, "Note.md")
        note.write_text("A")
        handler._push(note, "Note.md")

        assert client.push_vault_file.call_count == 3

    def test_failed_push_is_not_recorded_as_delivered(self, tmp_path):
        """A push that never landed must be retried, not suppressed."""
        handler, client = _handler(tmp_path, push_side_effect=RuntimeError("down"))
        note = _write(tmp_path, "Note.md", "hello")

        handler._push(note, "Note.md")
        assert "Note.md" not in handler._last_pushed

        # Isolate the re-push: clear the queue so the success path's own
        # _drain_retry_queue() doesn't add a second (legitimate) call and
        # blur what this test is pinning.
        handler._drain_timer.cancel()
        handler._retry_queue.clear()
        client.push_vault_file.reset_mock()
        client.push_vault_file.side_effect = None

        handler._push(note, "Note.md")
        assert client.push_vault_file.call_count == 1  # retried, not suppressed
        assert handler._last_pushed["Note.md"] is not None

    def test_retry_drain_records_delivery_too(self, tmp_path):
        """Landing via the queue counts — otherwise the next touch re-pushes."""
        handler, client = _handler(tmp_path)
        note = _write(tmp_path, "Note.md", "hello")
        import hashlib
        h = hashlib.md5(b"hello").hexdigest()
        handler._retry_queue.append(("Note.md", "hello", h))

        handler._drain_tick()
        assert handler._last_pushed["Note.md"] == h

        handler._push(note, "Note.md")
        assert client.push_vault_file.call_count == 1  # no second push
