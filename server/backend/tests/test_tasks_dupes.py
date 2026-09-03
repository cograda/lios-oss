"""Duplicate detection and merge for the task ledger (db tier).

Two properties matter more than the rest:

  - the vector tracks the task's *life*: open → embedded, closed → removed. A
    finished task appearing in a duplicates view, or in household search as if
    it were live, is the failure this exists to prevent;
  - an empty pair list is never silently "no duplicates" — the tool reports
    how many open tasks the index has not reached yet.

`near_duplicates` itself is exercised in `test_vector_native_similarity.py`;
here it is stubbed, because what is under test is the ledger's use of it.
"""

import json

import pytest

pytestmark = pytest.mark.db

SAMPLE = """---
title: Task Backlog
---

# Backlog — by category

# Home

## [[Malahide House]]

- [ ] **Wall mount the TV** 🔺 #home #quick
- [ ] **Mount the television on the wall** #home
- [ ] **Book the boiler service** #home
"""


@pytest.fixture
def ledger(db_session, tmp_path, monkeypatch):
    from app.integrations.tasks.importer import import_backlog

    note = tmp_path / "Task Backlog.md"
    note.write_text("placeholder\n")
    monkeypatch.setattr(
        "app.services.vault_paths.resolve", lambda path, user_id_override=None: note,
    )
    import_backlog(db_session, SAMPLE)
    return note


def _call(handler, session, **args):
    return json.loads(handler(session, args))


def _uid(session, title_fragment):
    from app.integrations.tasks.models import Task
    return session.query(Task).filter(Task.title.ilike(f"%{title_fragment}%")).one().uid


def _queue_rows(session, uid):
    from app.services.embedding import EmbeddingQueue
    return session.query(EmbeddingQueue).filter_by(source="task", source_id=uid).all()


def _embedding_rows(session, uid):
    from app.services.embedding import Embedding
    return session.query(Embedding).filter_by(source="task", source_id=uid).all()


def _seed_embedding(session, uid, text):
    """A stored vector row without a provider: what the processor would have
    left behind. Only the `embeddings` row is needed to prove deletion."""
    from app.services.embedding import Embedding, _content_hash
    session.add(Embedding(source="task", source_id=uid, chunk_text=text,
                          content_hash=_content_hash(text), user_id=None))
    session.commit()


# ─── the vector follows the task ───────────────────────────────────────────


def test_add_queues_the_task_for_embedding(ledger, db_session):
    from app.integrations.tasks.tools import tasks_add_handler

    out = _call(tasks_add_handler, db_session, title="Replace the porch light", description="The fitting is cracked.")
    rows = _queue_rows(db_session, out["created"]["uid"])
    assert len(rows) == 1
    assert "porch light" in rows[0].content and "cracked" in rows[0].content


def test_duplicates_backfills_open_tasks_and_reports_pending(ledger, db_session, monkeypatch):
    from app.integrations.tasks import dupes

    monkeypatch.setattr("app.services.embedding.EmbeddingService.near_duplicates", lambda *a, **k: [])
    out = _call(dupes.tasks_duplicates_handler, db_session)
    assert out["queued_now"] == 3
    assert out["pending_index"] == 3
    assert out["pairs"] == []
    assert "not indexed yet" in out["note"]
    # Second call queues nothing new — unchanged content is skipped.
    again = _call(dupes.tasks_duplicates_handler, db_session)
    assert again["queued_now"] == 0


def test_completing_removes_the_vector(ledger, db_session):
    from app.integrations.tasks.tools import tasks_complete_handler

    uid = _uid(db_session, "boiler")
    _seed_embedding(db_session, uid, "Book the boiler service")
    assert _embedding_rows(db_session, uid)
    _call(tasks_complete_handler, db_session, uid=uid)
    assert _embedding_rows(db_session, uid) == []


def test_editing_the_title_requeues(ledger, db_session):
    from app.integrations.tasks.tools import tasks_update_handler

    uid = _uid(db_session, "boiler")
    _seed_embedding(db_session, uid, "Book the boiler service")
    _call(tasks_update_handler, db_session, uid=uid, title="Book the boiler service before October")
    rows = _queue_rows(db_session, uid)
    assert len(rows) == 1 and "October" in rows[0].content


# ─── pairs ─────────────────────────────────────────────────────────────────


def test_pairs_exclude_closed_tasks_and_distinct_rulings(ledger, db_session, monkeypatch):
    from app.integrations.tasks import dupes
    from app.integrations.tasks.tools import tasks_complete_handler

    a, b, c = _uid(db_session, "Wall mount"), _uid(db_session, "television"), _uid(db_session, "boiler")
    fake = [
        {"a": a, "b": b, "score": 0.91},
        {"a": a, "b": c, "score": 0.86},   # c will be closed
        {"a": b, "b": c, "score": 0.85},   # will be ruled distinct
    ]
    monkeypatch.setattr("app.services.embedding.EmbeddingService.near_duplicates", lambda *a_, **k: fake)

    out = _call(dupes.tasks_duplicates_handler, db_session)
    assert {(p["a"], p["b"]) for p in out["pairs"]} == {tuple(sorted((a, b))), tuple(sorted((a, c))), tuple(sorted((b, c)))}

    _call(tasks_complete_handler, db_session, uid=c)
    _call(dupes.tasks_merge_handler, db_session, action="distinct", keep=b, drop=c)
    out = _call(dupes.tasks_duplicates_handler, db_session)
    assert [(p["a"], p["b"]) for p in out["pairs"]] == [tuple(sorted((a, b)))]
    assert out["pairs"][0]["a_title"] and out["pairs"][0]["score"] == 0.91


def test_distinct_ruling_is_order_independent(ledger, db_session, monkeypatch):
    from app.integrations.tasks import dupes

    a, b = _uid(db_session, "Wall mount"), _uid(db_session, "television")
    monkeypatch.setattr("app.services.embedding.EmbeddingService.near_duplicates",
                        lambda *a_, **k: [{"a": b, "b": a, "score": 0.9}])
    _call(dupes.tasks_merge_handler, db_session, action="distinct", keep=a, drop=b)
    assert _call(dupes.tasks_duplicates_handler, db_session)["pairs"] == []
    # Ruling twice is a no-op, not a second link.
    _call(dupes.tasks_merge_handler, db_session, action="distinct", keep=b, drop=a)
    from app.integrations.tasks.models import TaskLink
    assert db_session.query(TaskLink).filter_by(predicate="distinct_from").count() == 1


def test_threshold_is_bounded(ledger, db_session):
    from app.integrations.tasks import dupes
    with pytest.raises(ValueError):
        dupes.tasks_duplicates_handler(db_session, {"threshold": 0.2})


# ─── merge ─────────────────────────────────────────────────────────────────


def test_merge_folds_text_repoints_links_and_drops(ledger, db_session):
    from app.integrations.tasks import dupes
    from app.integrations.tasks.models import Task, TaskLink
    from app.integrations.tasks.tools import tasks_block_handler, tasks_query_handler, tasks_update_handler

    keep, drop, other = _uid(db_session, "Wall mount"), _uid(db_session, "television"), _uid(db_session, "boiler")
    _call(tasks_update_handler, db_session, uid=drop, description="Bracket is in the garage.", queue="focus")
    # `other` waits on the task about to be dropped.
    _call(tasks_block_handler, db_session, action="add", uid=other, blocked_by=drop)

    out = _call(dupes.tasks_merge_handler, db_session, keep=keep, drop=drop, title="Wall-mount the living room TV")
    assert out["kept"] == keep and out["dropped"] == drop and out["links_repointed"] == 1

    kept = db_session.query(Task).filter_by(uid=keep).one()
    dropped = db_session.query(Task).filter_by(uid=drop).one()
    assert kept.title == "Wall-mount the living room TV"
    assert f"Merged from {drop}" in kept.description and "garage" in kept.description
    assert kept.queue == "focus", "the stronger commitment wins"
    assert dropped.status == "dropped" and dropped.completed_at is not None
    assert db_session.query(TaskLink).filter_by(from_task_id=dropped.id, predicate="duplicates", target_ref=keep).count() == 1

    # The blocker now points at the kept task, so `other` is still blocked.
    blocked = _call(tasks_block_handler, db_session, action="list")["blocked"]
    assert blocked.get(other) == [keep]
    # And the dropped task has left the open ledger.
    open_uids = {t["uid"] for t in _call(tasks_query_handler, db_session)["tasks"]}
    assert drop not in open_uids and keep in open_uids
    # Rows now carry the description, so the app can show notes.
    row = next(t for t in _call(tasks_query_handler, db_session)["tasks"] if t["uid"] == keep)
    assert "garage" in row["description"]


def test_merge_refuses_self_and_closed(ledger, db_session):
    from app.integrations.tasks import dupes
    from app.integrations.tasks.tools import tasks_complete_handler

    a, b = _uid(db_session, "Wall mount"), _uid(db_session, "boiler")
    with pytest.raises(ValueError, match="same task"):
        dupes.tasks_merge_handler(db_session, {"keep": a, "drop": a})
    _call(tasks_complete_handler, db_session, uid=b)
    with pytest.raises(ValueError, match="already done"):
        dupes.tasks_merge_handler(db_session, {"keep": a, "drop": b})


def test_tools_are_registered(ledger):
    from app.integrations.tasks.tools import mcp_tools
    names = {t["name"] for t in mcp_tools()}
    assert {"tasks_duplicates", "tasks_merge"} <= names


# ─── split ─────────────────────────────────────────────────────────────────


def test_split_keeps_uid_and_inherits(ledger, db_session):
    from app.integrations.tasks.models import Task
    from app.integrations.tasks.tools import tasks_split_handler, tasks_update_handler

    uid = _uid(db_session, "Wall mount")
    _call(tasks_update_handler, db_session, uid=uid, queue="week", priority="high")
    out = _call(tasks_split_handler, db_session, uid=uid,
                parts=["Buy the TV bracket", "Drill and mount the bracket", "Hang the TV"])
    assert out["kept"]["uid"] == uid and out["kept"]["title"] == "Buy the TV bracket"
    assert [t["title"] for t in out["created"]] == ["Drill and mount the bracket", "Hang the TV"]
    for t in out["created"]:
        assert t["project"] == "Malahide House" and t["queue"] == "week" and t["priority"] == "high"
        assert t["source"] == "split" and t["uid"] != uid
    # Queued for embedding like any other open task.
    assert _queue_rows(db_session, out["created"][0]["uid"])
    assert db_session.query(Task).filter(Task.status == "next").count() == 5


def test_split_needs_two_parts(ledger, db_session):
    from app.integrations.tasks.tools import tasks_split_handler
    with pytest.raises(ValueError, match="two"):
        tasks_split_handler(db_session, {"uid": _uid(db_session, "boiler"), "parts": ["only one", "  "]})


# ─── the title signal, and similar-to-one ──────────────────────────────────


def test_near_identical_titles_pair_without_any_vector(ledger, db_session, monkeypatch):
    """The second signal. Two lines that say the same thing in nearly the same
    words are a pair even before the embedding processor has run — and even
    when a description on one side dilutes the semantic score."""
    from app.integrations.tasks import dupes
    from app.integrations.tasks.tools import tasks_add_handler

    monkeypatch.setattr("app.services.embedding.EmbeddingService.near_duplicates", lambda *a, **k: [])
    _call(tasks_add_handler, db_session, title="Fit a new lock on the utility-room gate", project="Malahide House")
    _call(tasks_add_handler, db_session, title="Fit the new lock to the gate by the utility room", project="Malahide House")
    out = _call(dupes.tasks_duplicates_handler, db_session)
    assert out["pair_count"] == 1
    p = out["pairs"][0]
    assert p["signal"] == "title" and p["same_project"] is True and p["score"] >= 0.6


def test_a_pair_found_both_ways_ranks_first(ledger, db_session, monkeypatch):
    from app.integrations.tasks import dupes
    from app.integrations.tasks.tools import tasks_add_handler

    b = _uid(db_session, "boiler")          # "Book the boiler service"
    a = _call(tasks_add_handler, db_session, title="Book the boiler service before winter", project="Malahide House")["created"]["uid"]
    c, d = _uid(db_session, "Wall mount"), _uid(db_session, "television")
    monkeypatch.setattr("app.services.embedding.EmbeddingService.near_duplicates",
                        lambda *a_, **k: [{"a": c, "b": d, "score": 0.95}, {"a": a, "b": b, "score": 0.81}])
    out = _call(dupes.tasks_duplicates_handler, db_session)
    signals = {(p["a"], p["b"]): p["signal"] for p in out["pairs"]}
    assert signals[tuple(sorted((a, b)))] == "both"
    # Ordering: same project first, then pairs both signals agree on, then score.
    assert out["pairs"][0]["signal"] == "both", "semantic+title beats a higher semantic-only score within a project"


def test_similar_to_one_task_merges_signals_and_honours_distinct(ledger, db_session, monkeypatch):
    from app.integrations.tasks import dupes

    a, b, c = _uid(db_session, "Wall mount"), _uid(db_session, "television"), _uid(db_session, "boiler")
    monkeypatch.setattr("app.services.embedding.EmbeddingService.similar_to",
                        lambda *a_, **k: [{"source_id": b, "score": 0.9}, {"source_id": c, "score": 0.7}])
    out = _call(dupes.tasks_similar_handler, db_session, uid=a)
    by = {r["uid"]: r for r in out["similar"]}
    assert by[b]["signal"] in ("semantic", "both") and by[b]["score"] >= 0.9
    assert by[c]["signal"] == "semantic" and by[c]["project"] == "Malahide House"
    # A ruling removes the neighbour from the answer.
    _call(dupes.tasks_merge_handler, db_session, action="distinct", keep=a, drop=c)
    assert c not in {r["uid"] for r in _call(dupes.tasks_similar_handler, db_session, uid=a)["similar"]}
