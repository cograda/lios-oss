"""User-scoping of the unified embedding pipeline.

Basic unit tier — verifies user_id is threaded through enqueue and
process_queue, and the context accessor behaves. The full enforcement
suite (real Postgres, two seeded users, every search path) lands with
the testcontainers harness in Phase 1.
"""

from unittest.mock import MagicMock, patch

from app.auth.context import _current_user_id, current_user_id_or_none, use_user
from app.services.embedding import Embedding, EmbeddingQueue, EmbeddingService


def test_current_user_id_or_none_unbound_returns_none():
    # conftest pins user 1 for every test — unwind to the unbound sentinel.
    token = _current_user_id.set(0)
    try:
        assert current_user_id_or_none() is None
    finally:
        _current_user_id.reset(token)


def test_current_user_id_or_none_bound_returns_id():
    with use_user(7):
        assert current_user_id_or_none() == 7


def _empty_session():
    """Mock session whose dedup lookups find nothing."""
    session = MagicMock()
    query = MagicMock()
    session.query.return_value = query
    query.filter_by.return_value = query
    query.first.return_value = None
    return session


def test_enqueue_stores_user_id():
    session = _empty_session()
    assert EmbeddingService.enqueue(
        session, "email", "msg-1", "hello", user_id=2
    )
    queued = session.add.call_args[0][0]
    assert isinstance(queued, EmbeddingQueue)
    assert queued.user_id == 2


def test_enqueue_defaults_to_shared():
    session = _empty_session()
    assert EmbeddingService.enqueue(session, "vault", "Note.md", "hello")
    queued = session.add.call_args[0][0]
    assert queued.user_id is None


def test_enqueue_batch_applies_user_id_to_all_items():
    session = _empty_session()
    count = EmbeddingService.enqueue_batch(
        session,
        [("whatsapp", "c:1:2", "text a", None), ("whatsapp", "c:3:4", "text b", None)],
        user_id=2,
    )
    assert count == 2
    added = [call[0][0] for call in session.add.call_args_list]
    assert all(item.user_id == 2 for item in added)


def _queue_item(source_id, content="hello", user_id=None):
    return EmbeddingQueue(
        source="email",
        source_id=source_id,
        user_id=user_id,
        content=content,
        content_hash=f"hash-{source_id}",
        status="processing",
        attempts=0,
    )


def test_bisect_isolates_poison_item():
    """A failing item must not take its batch cohort down with it."""
    items = [_queue_item("good-1"), _queue_item("poison"), _queue_item("good-2")]
    session = MagicMock()
    query = MagicMock()
    session.query.return_value = query
    query.filter_by.return_value = query

    def embed(texts):
        if any("POISON" in t for t in texts):
            raise RuntimeError("bad input")
        return [[0.0] * 384 for _ in texts]

    items[1].content = "POISON"
    with patch("app.services.embedding._embed_via_subprocess", side_effect=embed):
        done = EmbeddingService._embed_and_store(session, items)

    assert done == 2
    assert items[0].status == "done"
    assert items[2].status == "done"
    assert items[1].status == "pending"  # retried next cycle
    assert items[1].attempts == 1


def test_poison_item_errors_after_max_attempts():
    item = _queue_item("poison")
    item.attempts = 2  # two strikes already
    session = MagicMock()

    with patch(
        "app.services.embedding._embed_via_subprocess",
        side_effect=RuntimeError("bad input"),
    ):
        done = EmbeddingService._embed_and_store(session, [item])

    assert done == 0
    assert item.status == "error"
    assert item.attempts == 3
    assert "bad input" in item.error_message


def test_transient_batch_failure_marks_all_pending_for_retry():
    """A batch-wide failure (e.g. OOM) re-queues everything, errors nothing."""
    items = [_queue_item("a"), _queue_item("b")]
    session = MagicMock()

    with patch(
        "app.services.embedding._embed_via_subprocess",
        side_effect=RuntimeError("subprocess died"),
    ):
        done = EmbeddingService._embed_and_store(session, items)

    assert done == 0
    assert all(i.status == "pending" for i in items)
    assert all(i.attempts == 1 for i in items)


def test_process_queue_copies_user_id_to_embedding():
    item = EmbeddingQueue(
        source="email",
        source_id="msg-1",
        user_id=2,
        content="hello",
        content_hash="abc",
        status="pending",
    )
    session = MagicMock()
    query = MagicMock()
    session.query.return_value = query
    query.filter_by.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.all.return_value = [item]
    query.filter.return_value = query

    with patch(
        "app.services.embedding._embed_via_subprocess",
        return_value=[[0.0] * 384],
    ):
        processed = EmbeddingService.process_queue(session)

    assert processed == 1
    embeddings = [
        call[0][0]
        for call in session.add.call_args_list
        if isinstance(call[0][0], Embedding)
    ]
    assert len(embeddings) == 1
    assert embeddings[0].user_id == 2
    assert item.status == "done"
