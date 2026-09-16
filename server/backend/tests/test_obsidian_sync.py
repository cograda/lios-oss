"""db-tier tests for `app.integrations.obsidian.sync` — chunked indexing,
stale-chunk cleanup, and the chunker-version cache-bust.

These exercise the real enqueue/dedup path against Postgres (no fastembed —
enqueuing lands rows in `embedding_queue`; nothing here needs a vector).
"""

import hashlib
from pathlib import Path

import pytest

from app.integrations.obsidian import chunking as chunking_mod
from app.integrations.obsidian.models import VaultChunk
from app.integrations.obsidian.sync import (
    _versioned_hash,
    index_single_file,
    index_vault,
)
from app.services.embedding import Embedding, EmbeddingQueue

pytestmark = pytest.mark.db


def _write(vault_dir: Path, rel_path: str, content: str) -> Path:
    p = vault_dir / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _big_list_file(n_headings: int = 3, items_per_heading: int = 12) -> str:
    lines = ["# Backlog"]
    for h in range(n_headings):
        lines.append(f"\n## Section {h}")
        for i in range(items_per_heading):
            lines.append(
                f"- [ ] **Task {h}-{i}** — a moderately long description of "
                f"the work involved, enough text to be realistic #tag{h}"
            )
    return "\n".join(lines)


def _vault_source_ids(session, user_id: int) -> set[str]:
    return {
        row[0]
        for row in session.query(Embedding.source_id)
        .filter_by(source="vault", user_id=user_id)
        .all()
    }


def _queue_source_ids(session, user_id: int) -> set[str]:
    """source_ids currently *pending* in the queue for this user's vault.

    Deliberately excludes done/error rows — a test marking an item "done"
    to simulate the worker having processed it should not still count as
    "queued" here.
    """
    return {
        row[0]
        for row in session.query(EmbeddingQueue.source_id)
        .filter_by(source="vault", user_id=user_id, status="pending")
        .all()
    }


class TestIndexVaultChunking:
    def test_large_list_file_produces_multiple_chunk_ids(self, db_session, tmp_path):
        vault = tmp_path
        _write(vault, "Task Backlog.md", _big_list_file())

        result = index_vault(db_session, str(vault), user_id=1)

        assert result["files_total"] == 1
        assert result["enqueued"] == 1
        assert result["chunks_enqueued"] > 1

        ids = _queue_source_ids(db_session, 1)
        assert all(i.startswith("Task Backlog.md#") for i in ids)
        assert len(ids) == result["chunks_enqueued"]

    def test_full_scan_skips_syncthing_version_history(self, db_session, tmp_path):
        """A `.stversions` snapshot sitting beside its live file is ignored.

        This is the measured regression: both files hold near-identical text,
        so the snapshot scored within a hair of the original and sometimes
        above it. Only the live file should ever reach the index.
        """
        vault = tmp_path
        content = _big_list_file()
        _write(vault, "Task Backlog.md", content)
        _write(vault, ".stversions/Task Backlog~20260801-112211.md", content)

        stats = index_vault(db_session, str(vault), user_id=1)

        assert stats["files_total"] == 1
        assert not any(
            sid.startswith(".stversions/") for sid in _queue_source_ids(db_session, 1)
        )

    def test_small_prose_file_keeps_bare_path_source_id(self, db_session, tmp_path):
        vault = tmp_path
        _write(vault, "Daily Notes/2026-08-13.md", "# Daily\n\nJust a short note.")

        result = index_vault(db_session, str(vault), user_id=1)

        assert result["chunks_enqueued"] == 1
        ids = _queue_source_ids(db_session, 1)
        assert ids == {"Daily Notes/2026-08-13.md"}

    def test_unchanged_file_is_reused_not_reenqueued(self, db_session, tmp_path):
        vault = tmp_path
        _write(vault, "Task Backlog.md", _big_list_file())

        index_vault(db_session, str(vault), user_id=1)
        # Mark the queued items done, as if the worker had processed them,
        # so a second pass can't "reuse" via queue-dedup alone.
        db_session.query(EmbeddingQueue).update({"status": "done"})
        db_session.commit()

        result = index_vault(db_session, str(vault), user_id=1)
        assert result["reused"] == 1
        assert result["enqueued"] == 0

    def test_removed_file_cleans_up_all_its_chunks(self, db_session, tmp_path):
        vault = tmp_path
        path = _write(vault, "Task Backlog.md", _big_list_file())
        index_vault(db_session, str(vault), user_id=1)

        # Simulate the worker having embedded every queued chunk.
        for item in db_session.query(EmbeddingQueue).filter_by(source="vault").all():
            db_session.add(Embedding(
                source="vault", source_id=item.source_id, user_id=1,
                chunk_text=item.content, content_hash=item.content_hash,
            ))
        db_session.query(EmbeddingQueue).delete()
        db_session.commit()

        before = _vault_source_ids(db_session, 1)
        assert len(before) > 1

        path.unlink()
        result = index_vault(db_session, str(vault), user_id=1)

        assert result["removed"] == 1
        assert result["stale_chunks_removed"] == len(before)
        assert _vault_source_ids(db_session, 1) == set()

    def test_rechunk_removes_stale_chunk_ids_when_count_shrinks(self, db_session, tmp_path):
        vault = tmp_path
        path = _write(vault, "Task Backlog.md", _big_list_file(n_headings=5, items_per_heading=15))
        index_vault(db_session, str(vault), user_id=1)

        for item in db_session.query(EmbeddingQueue).filter_by(source="vault").all():
            db_session.add(Embedding(
                source="vault", source_id=item.source_id, user_id=1,
                chunk_text=item.content, content_hash=item.content_hash,
            ))
        db_session.query(EmbeddingQueue).delete()
        db_session.commit()

        big_count = len(_vault_source_ids(db_session, 1))
        assert big_count > 3

        # Shrink the file drastically — still above the whole-file threshold
        # and still list-structured, but far fewer chunks.
        path.write_text(_big_list_file(n_headings=1, items_per_heading=12), encoding="utf-8")
        index_vault(db_session, str(vault), user_id=1)

        remaining = _vault_source_ids(db_session, 1) | _queue_source_ids(db_session, 1)
        assert len(remaining) < big_count
        # None of the old high-index chunk ids should survive.
        assert not any(i.endswith(f"#{big_count - 1}") for i in remaining)

    def test_file_crossing_into_chunked_mode_drops_old_bare_path_id(self, db_session, tmp_path):
        vault = tmp_path
        path = _write(vault, "Notes.md", "# Notes\n\nshort note for now")
        index_vault(db_session, str(vault), user_id=1)
        assert _queue_source_ids(db_session, 1) == {"Notes.md"}

        # Mark done so it's a real Embedding row, then grow the file past
        # the chunking threshold.
        item = db_session.query(EmbeddingQueue).filter_by(source="vault").first()
        db_session.add(Embedding(
            source="vault", source_id=item.source_id, user_id=1,
            chunk_text=item.content, content_hash=item.content_hash,
        ))
        db_session.query(EmbeddingQueue).delete()
        db_session.commit()

        path.write_text(_big_list_file(), encoding="utf-8")
        index_vault(db_session, str(vault), user_id=1)

        remaining_embeddings = _vault_source_ids(db_session, 1)
        assert "Notes.md" not in remaining_embeddings  # bare id is stale now
        queued = _queue_source_ids(db_session, 1)
        assert all(i.startswith("Notes.md#") for i in queued)


class TestChunkerVersionCacheBust:
    def test_bumping_chunker_version_forces_reindex_of_unchanged_file(
        self, db_session, tmp_path, monkeypatch,
    ):
        vault = tmp_path
        _write(vault, "Task Backlog.md", _big_list_file())

        index_vault(db_session, str(vault), user_id=1)
        db_session.query(EmbeddingQueue).update({"status": "done"})
        db_session.commit()

        # Content unchanged, same version → reused.
        result = index_vault(db_session, str(vault), user_id=1)
        assert result["reused"] == 1

        # Bump the chunker version — the stored (versioned) hash no longer
        # matches, so the file must be treated as changed even though the
        # file bytes are identical.
        monkeypatch.setattr(chunking_mod, "CHUNKER_VERSION", chunking_mod.CHUNKER_VERSION + 1)
        # sync.py imports CHUNKER_VERSION by name, so patch it there too.
        import app.integrations.obsidian.sync as sync_mod
        monkeypatch.setattr(sync_mod, "CHUNKER_VERSION", chunking_mod.CHUNKER_VERSION)

        result = index_vault(db_session, str(vault), user_id=1)
        assert result["reused"] == 0
        assert result["enqueued"] == 1

    def test_versioned_hash_differs_by_version(self):
        raw = hashlib.md5(b"hello").hexdigest()
        v1 = _versioned_hash(raw)
        assert v1 == f"{raw}:cv{chunking_mod.CHUNKER_VERSION}"


class TestIndexSingleFile:
    def test_chunks_large_file_and_cleans_stale_ids_on_rechunk(self, db_session):
        content = _big_list_file(n_headings=4, items_per_heading=12)
        h = hashlib.md5(content.encode()).hexdigest()

        index_single_file(db_session, "Task Backlog.md", content, h, user_id=1)
        ids = _queue_source_ids(db_session, 1)
        assert len(ids) > 1
        assert all(i.startswith("Task Backlog.md#") for i in ids)

        for item in db_session.query(EmbeddingQueue).filter_by(source="vault").all():
            db_session.add(Embedding(
                source="vault", source_id=item.source_id, user_id=1,
                chunk_text=item.content, content_hash=item.content_hash,
            ))
        db_session.query(EmbeddingQueue).delete()
        db_session.commit()
        big_count = len(ids)

        shrunk = _big_list_file(n_headings=1, items_per_heading=10)
        h2 = hashlib.md5(shrunk.encode()).hexdigest()
        index_single_file(db_session, "Task Backlog.md", shrunk, h2, user_id=1)

        remaining = _vault_source_ids(db_session, 1) | _queue_source_ids(db_session, 1)
        assert len(remaining) < big_count

    def test_same_hash_skips_reindex(self, db_session):
        content = "# Note\n\nshort"
        h = hashlib.md5(content.encode()).hexdigest()
        index_single_file(db_session, "x.md", content, h, user_id=1)
        assert _queue_source_ids(db_session, 1) == {"x.md"}

        db_session.query(EmbeddingQueue).update({"status": "done"})
        db_session.commit()

        # Same content, same hash — must not re-enqueue.
        index_single_file(db_session, "x.md", content, h, user_id=1)
        assert _queue_source_ids(db_session, 1) == set()

    def test_excluded_path_is_refused_at_the_chokepoint(self, db_session):
        """`/api/v1/vault/push` has no filter of its own — this is the one.

        The endpoint hands whatever the daemon sends straight to
        `index_single_file`, so before 2026-08-14 a `.stversions` file could be
        pushed past the walker's exclusion entirely. Enforcing here means no
        caller can bypass the rule by forgetting it.
        """
        content = _big_list_file()
        h = hashlib.md5(content.encode()).hexdigest()

        index_single_file(
            db_session, ".stversions/Task Backlog~20260801-112211.md",
            content, h, user_id=1,
        )

        assert _queue_source_ids(db_session, 1) == set()
        assert db_session.query(VaultChunk).filter_by(user_id=1).count() == 0

    def test_two_users_same_path_do_not_cross_contaminate(self, db_session):
        content = _big_list_file()
        h = hashlib.md5(content.encode()).hexdigest()

        index_single_file(db_session, "Task Backlog.md", content, h, user_id=1)
        index_single_file(db_session, "Task Backlog.md", content, h, user_id=2)

        assert _queue_source_ids(db_session, 1) == _queue_source_ids(db_session, 2)
        user1_rows = (
            db_session.query(VaultChunk).filter_by(user_id=1, path="Task Backlog.md").count()
        )
        user2_rows = (
            db_session.query(VaultChunk).filter_by(user_id=2, path="Task Backlog.md").count()
        )
        assert user1_rows == 1
        assert user2_rows == 1
