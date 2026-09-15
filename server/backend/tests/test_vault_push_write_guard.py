"""unit-tier tests for the `/api/v1/vault/push` write guard.

The endpoint wrote `payload.content` to disk unconditionally. That was an
infinite loop, because two transports own the vault tree at once: Syncthing
replicates it laptop<->server, and this endpoint writes into the same tree.
Rewriting identical bytes still bumps mtime, and the Mac's watcher fires on
metadata changes, so:

    daemon pushes X -> server rewrites X -> Syncthing carries the fresh mtime
      to the Mac -> fsevents fires -> daemon pushes X -> ...

Found live 2026-08-14 running at roughly one lap per 8-10s over the 17 files
that had ever been pushed, active since at least 09 Aug. It cost no embeddings
— `index_single_file` dedups on `file_hash` — but it manufactured Syncthing
conflict files (two writers, one file) and made mtime meaningless across the
working set, which is precisely what `vault_recent` reports on.

So the assertion that matters is about **mtime**, not content: a guard that
rewrites identical bytes "harmlessly" still feeds the loop.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

OLD_NS = 1_600_000_000_000_000_000  # a fixed mtime, comfortably in the past


@pytest.fixture
def push(tmp_path, monkeypatch):
    """Call vault_push against a throwaway vault, with the DB/indexer stubbed."""
    from app.api import v1
    from app.services import vault_paths

    vault_root = tmp_path / "alex"
    vault_root.mkdir()
    monkeypatch.setattr(vault_paths, "user_vault_path", lambda name: vault_root)

    indexed = []
    import app.integrations.obsidian.sync as sync_mod
    monkeypatch.setattr(
        sync_mod, "index_single_file",
        lambda session, path, content, file_hash, user_id: indexed.append(path),
    )

    db = MagicMock()
    db.session.return_value.__enter__ = lambda s: MagicMock()
    db.session.return_value.__exit__ = lambda s, *a: False
    monkeypatch.setattr(v1, "get_db", lambda: db)

    user = MagicMock()
    user.name = "alex"
    user.id = 1

    def _call(rel_path: str, content: str):
        payload = v1.VaultPushRequest(
            path=rel_path, content=content,
            file_hash=__import__("hashlib").md5(content.encode()).hexdigest(),
        )
        return v1.vault_push(payload, user=user)

    _call.vault_root = vault_root
    _call.indexed = indexed
    return _call


def _age(p: Path) -> None:
    os.utime(p, ns=(OLD_NS, OLD_NS))


class TestWriteGuard:
    def test_identical_content_does_not_touch_mtime(self, push):
        """The loop-breaker. Same bytes in ⇒ the file is not written at all."""
        target = push.vault_root / "Note.md"
        target.write_text("same bytes", encoding="utf-8")
        _age(target)

        push("Note.md", "same bytes")

        assert target.stat().st_mtime_ns == OLD_NS, (
            "identical content rewrote the file; Syncthing will replicate the "
            "new mtime and the push loop resumes"
        )

    def test_changed_content_is_written(self, push):
        target = push.vault_root / "Note.md"
        target.write_text("before", encoding="utf-8")
        _age(target)

        push("Note.md", "after")

        assert target.read_text(encoding="utf-8") == "after"
        assert target.stat().st_mtime_ns != OLD_NS

    def test_new_file_is_created(self, push):
        push("Nested/New.md", "hello")
        assert (push.vault_root / "Nested" / "New.md").read_text() == "hello"

    def test_indexing_still_runs_for_an_unchanged_file(self, push):
        """Skipping the *write* must not skip the *index*.

        The push exists to make indexing prompt. A file can be byte-identical
        on disk and still absent from the index — after a restore, a reindex,
        or a chunker change — so the guard covers the write only.
        """
        target = push.vault_root / "Note.md"
        target.write_text("same bytes", encoding="utf-8")
        _age(target)

        push("Note.md", "same bytes")

        assert push.indexed == ["Note.md"]


class TestContainmentStillHolds:
    """The guard added a read before the write; the escape checks precede both."""

    @pytest.mark.parametrize("bad", ["../../etc/passwd", "a/../../b"])
    def test_traversal_is_rejected(self, push, bad):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            push(bad, "x")
        assert exc.value.status_code == 400

    def test_a_leading_slash_is_contained_not_rejected(self, push):
        """`lstrip("/")` makes an absolute-looking path relative, deliberately.

        So `/etc/passwd` lands at `<vault>/etc/passwd` — odd, but inside the
        vault, which is the property that matters. Pinned because the obvious
        reading of the handler is that it rejects this, and a future
        "hardening" pass might make it do so without noticing that clients
        legitimately send leading slashes.
        """
        push("/etc/passwd", "x")
        written = push.vault_root / "etc" / "passwd"
        assert written.read_text() == "x"
        assert str(written).startswith(str(push.vault_root) + "/")
