"""Client vault watcher — retry queue and periodic drain."""

from collections import deque
from unittest.mock import MagicMock

from comar.vault_watcher import MAX_RETRY_QUEUE, VaultHandler


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
