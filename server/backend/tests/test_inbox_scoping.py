"""Scoping enforcement for the inbox integration (security finding F6).

Before 2026-08-08 the inbox had no user scoping at all: one flat on-disk
tree, and any valid bearer could list/preview/route any item via
`inbox_pending`/`inbox_preview`/etc. and the routing tools. This suite is
modeled on `tests/test_user_scoping.py`'s enforcement style (seed both
users' data, act as one, assert the other's marker never leaks) but at the
unit tier — a sqlite-backed fake `get_db()` stands in for the real
`InboxItem` DB path (`record_item`/`find_by_hash`/`adopt_legacy_files`),
and `tmp_path` stands in for the `/inbox` volume, so none of this needs the
`db` marker's Postgres testcontainer.

U1_MARKER / U2_MARKER mirror test_user_scoping.py's canary convention.
"""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.context import use_user
from app.integrations.inbox import scan
from app.integrations.inbox.models import InboxItem
from app.models.users import User

U1_MARKER = "SCOPETEST-OWN-DATA-U1"
U2_MARKER = "LEAK-CANARY-U2"


# ---------------------------------------------------------------------------
# Fixtures: fake filesystem root + fake DB, no Postgres needed
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_inbox_root(tmp_path, monkeypatch):
    """Point `scan.inbox_root()` at a throwaway directory."""
    monkeypatch.setattr(scan.settings, "inbox_path", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_db(monkeypatch):
    """A sqlite-backed stand-in for `app.db.get_db()`, scoped to just the
    two tables `InboxItem` touches (`users`, `inbox_items`) — avoids pulling
    in every other integration's pgvector-typed columns via a full
    `Base.metadata.create_all()`."""
    # `StaticPool` + `check_same_thread=False`: this fixture is also used by
    # `TestIngestSchedulesTranscription`'s `TestClient`, which runs the
    # request handler on a separate anyio worker thread. A plain
    # `sqlite:///:memory:` engine hands each new connection its own private
    # in-memory database, so a connection opened from that other thread
    # would see zero tables ("no such table: inbox_items") despite this
    # fixture having just created them — StaticPool keeps every connection,
    # from every thread, on the one already-populated database.
    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    User.__table__.create(engine)
    InboxItem.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)

    class _FakeDb:
        @contextmanager
        def session(self):
            s = session_factory()
            try:
                yield s
            finally:
                s.close()

    db = _FakeDb()
    with db.session() as s:
        s.add(User(id=1, name="alex", display_name="Alex"))
        s.add(User(id=2, name="sam", display_name="Sam"))
        s.commit()

    import app.db as db_mod

    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    return db


def _drop(root, user_id: int, bucket: str, filename: str, text: str, sha256: str | None = None) -> None:
    """Write one file + minimal sidecar directly, bypassing the ingest route
    (this suite is about the read/route side, not re-testing base64 decode)."""
    d = scan.user_root(user_id) / bucket
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_text(text)
    # Deliberately no `enriched_at` — leaving enrichment to run for real
    # (`extract_preview` reading the actual file) means the marker text
    # shows up wherever a real caller's would, rather than being hand-typed
    # into a sidecar preview field that production code never produces this
    # way.
    scan.write_sidecar(p, {"sha256": sha256} if sha256 else {})
    return p


# ---------------------------------------------------------------------------
# scan.py primitives
# ---------------------------------------------------------------------------


class TestPathScoping:
    def test_user_root_partitions_by_user_id(self, fake_inbox_root):
        assert scan.user_root(1) == fake_inbox_root / "u1"
        assert scan.user_root(2) == fake_inbox_root / "u2"
        assert scan.user_root(1) != scan.user_root(2)

    def test_safe_resolve_accepts_own_absolute_path(self, fake_inbox_root):
        p = _drop(fake_inbox_root, 1, "incoming", "a.txt", U1_MARKER)
        resolved = scan.safe_resolve(str(p), 1)
        assert resolved == p

    def test_safe_resolve_rejects_other_users_absolute_path(self, fake_inbox_root):
        """The core F6 enforcement point: user 2 handing in user 1's
        absolute path must not resolve to it."""
        p = _drop(fake_inbox_root, 1, "incoming", "a.txt", U1_MARKER)
        with pytest.raises(ValueError, match="escapes"):
            scan.safe_resolve(str(p), 2)

    def test_safe_resolve_rejects_traversal_out_of_own_root(self, fake_inbox_root):
        with pytest.raises(ValueError, match="escapes"):
            scan.safe_resolve("../u2/incoming/a.txt", 1)


class TestInternalArtifactsStayHidden:
    """A transcription lock must never surface as a captured item.

    Regression guard. Every walker used to filter on `_is_sidecar` alone, so
    once `_acquire_transcription_lock` started dropping a `.transcribing.lock`
    beside the audio, that lock read as a pending inbox item — enriched, given
    a sidecar of its own, and listed to the user as an empty mystery file.
    It self-heals within a minute in the happy path, which is exactly what
    would have made it maddening to reproduce from a bug report.
    """

    def test_lock_file_is_not_a_pending_item(self, fake_inbox_root):
        audio = _drop(fake_inbox_root, 1, "audio", "memo.m4a", U1_MARKER)
        lock = scan._transcription_lock_path(audio)
        lock.touch()
        assert lock.exists(), "precondition: the lock is really on disk"

        assert [p.name for p in scan.iter_pending_files(1)] == ["memo.m4a"]
        assert [p.name for p in scan.iter_all_pending_files()] == ["memo.m4a"]

    def test_lock_path_and_filter_agree(self, fake_inbox_root):
        """The constant is shared, so these two can't drift apart."""
        for name in ("memo.m4a", "no-extension", "two.dots.wav"):
            audio = _drop(fake_inbox_root, 1, "audio", name, U1_MARKER)
            assert scan._is_internal_artifact(scan._transcription_lock_path(audio))

    def test_sidecar_is_still_hidden(self, fake_inbox_root):
        """The behaviour the old filter had must survive the widening."""
        audio = _drop(fake_inbox_root, 1, "audio", "memo.m4a", U1_MARKER)
        scan.sidecar_path(audio).write_text("{}")
        assert [p.name for p in scan.iter_pending_files(1)] == ["memo.m4a"]


class TestIterAndListScoping:
    def test_iter_pending_files_is_scoped(self, fake_inbox_root):
        _drop(fake_inbox_root, 1, "incoming", "u1.txt", U1_MARKER)
        _drop(fake_inbox_root, 2, "incoming", "u2.txt", U2_MARKER)

        u1_files = scan.iter_pending_files(1)
        u2_files = scan.iter_pending_files(2)

        assert [p.name for p in u1_files] == ["u1.txt"]
        assert [p.name for p in u2_files] == ["u2.txt"]

    def test_iter_all_pending_files_spans_every_user(self, fake_inbox_root):
        """The background sweeps' helper — deliberately NOT scoped, since
        it never returns content to a specific caller."""
        _drop(fake_inbox_root, 1, "incoming", "u1.txt", U1_MARKER)
        _drop(fake_inbox_root, 2, "incoming", "u2.txt", U2_MARKER)

        names = {p.name for p in scan.iter_all_pending_files()}
        assert names == {"u1.txt", "u2.txt"}

    def test_list_pending_never_returns_other_users_item(self, fake_inbox_root, fake_db):
        _drop(fake_inbox_root, 1, "incoming", "u1.txt", U1_MARKER)
        _drop(fake_inbox_root, 2, "incoming", "u2.txt", U2_MARKER)

        as_user2 = scan.list_pending(2, limit=50)
        dump = json.dumps(as_user2, default=str)
        assert U1_MARKER not in dump
        assert any("u2.txt" in item["path"] for item in as_user2)


class TestFindByHashScoping:
    def test_dedup_never_matches_another_users_upload(self, fake_inbox_root, fake_db):
        """F6's dedup-specific risk: without scoping, a duplicate-check
        response leaks the existence AND path of another user's file."""
        digest = "deadbeef" * 8
        _drop(fake_inbox_root, 1, "incoming", "secret.txt", U1_MARKER, sha256=digest)
        scan.record_item(1, "incoming/secret.txt", sha256=digest)

        assert scan.find_by_hash(digest, 1) is not None
        assert scan.find_by_hash(digest, 2) is None

    def test_dedup_survives_a_bucket_transition(self, fake_inbox_root, fake_db):
        """`move_to` must re-record ownership at the new relative path, or a
        memo already archived would look "new" again on a re-post (the
        exact regression `find_by_hash`'s docstring warns about)."""
        digest = "cafebabe" * 8
        p = _drop(fake_inbox_root, 1, "incoming", "memo.m4a", U1_MARKER, sha256=digest)
        scan.record_item(1, "incoming/memo.m4a", sha256=digest)

        scan.move_to(p, "archive", 1)

        found = scan.find_by_hash(digest, 1)
        assert found is not None
        assert found.parent.name == "archive"


class TestLegacyAdoption:
    def test_adopt_legacy_files_moves_flat_tree_into_user1(self, fake_inbox_root, fake_db):
        flat_dir = fake_inbox_root / "incoming"
        flat_dir.mkdir(parents=True)
        legacy = flat_dir / "old.txt"
        legacy.write_text(U1_MARKER)

        moved = scan.adopt_legacy_files()

        assert moved == 1
        assert not legacy.exists()
        adopted = scan.user_root(1) / "incoming" / "old.txt"
        assert adopted.exists()
        assert adopted.read_text() == U1_MARKER

    def test_adopted_files_are_invisible_to_other_users(self, fake_inbox_root, fake_db):
        flat_dir = fake_inbox_root / "incoming"
        flat_dir.mkdir(parents=True)
        (flat_dir / "old.txt").write_text(U1_MARKER)

        # list_pending(1, ...) is the on-demand trigger for adoption.
        scan.list_pending(1, limit=50)

        as_user2 = scan.list_pending(2, limit=50)
        assert as_user2 == []

    def test_adoption_is_idempotent(self, fake_inbox_root, fake_db):
        flat_dir = fake_inbox_root / "incoming"
        flat_dir.mkdir(parents=True)
        (flat_dir / "old.txt").write_text(U1_MARKER)

        first = scan.adopt_legacy_files()
        second = scan.adopt_legacy_files()

        assert first == 1
        assert second == 0


# ---------------------------------------------------------------------------
# MCP tool handlers — the surface a real caller actually goes through
# ---------------------------------------------------------------------------


class TestToolHandlerScoping:
    def test_inbox_pending_leaks_no_cross_user_data(self, fake_inbox_root, fake_db, mock_session):
        from app.integrations.inbox.tools import handle_pending

        _drop(fake_inbox_root, 1, "incoming", "u1.txt", U1_MARKER)
        _drop(fake_inbox_root, 2, "incoming", "u2.txt", U2_MARKER)

        with use_user(2):
            out = handle_pending(mock_session, {"limit": 20})

        assert U1_MARKER not in out
        assert U2_MARKER in out

    def test_inbox_preview_refuses_other_users_path(self, fake_inbox_root, fake_db, mock_session):
        from app.integrations.inbox.tools import handle_preview

        p = _drop(fake_inbox_root, 1, "text", "u1.txt", U1_MARKER)

        with use_user(2):
            out = handle_preview(mock_session, {"path": str(p)})

        result = json.loads(out)
        assert "error" in result
        assert U1_MARKER not in out

    def test_inbox_archive_refuses_other_users_path(self, fake_inbox_root, fake_db, mock_session):
        from app.integrations.inbox.tools import handle_archive

        p = _drop(fake_inbox_root, 1, "incoming", "u1.txt", U1_MARKER)

        with use_user(2):
            out = handle_archive(mock_session, {"path": str(p)})

        result = json.loads(out)
        assert "error" in result
        # The file must not have moved — a scoping failure here would be a
        # write, not just a read, leak.
        assert p.exists()

    def test_inbox_dismiss_refuses_other_users_path(self, fake_inbox_root, fake_db, mock_session):
        from app.integrations.inbox.tools import handle_dismiss

        p = _drop(fake_inbox_root, 1, "incoming", "u1.txt", U1_MARKER)

        with use_user(2):
            out = handle_dismiss(mock_session, {"path": str(p)})

        result = json.loads(out)
        assert "error" in result
        assert p.exists()

    def test_owner_can_still_act_on_their_own_item(self, fake_inbox_root, fake_db, mock_session):
        from app.integrations.inbox.tools import handle_archive

        p = _drop(fake_inbox_root, 1, "incoming", "u1.txt", U1_MARKER)

        with use_user(1):
            out = handle_archive(mock_session, {"path": str(p)})

        result = json.loads(out)
        assert result["ok"] is True
        assert not p.exists()


# ---------------------------------------------------------------------------
# Ingest attribution — `POST /api/inbox/ingest` resolves the caller, never
# a hardcoded user
# ---------------------------------------------------------------------------


class TestIngestAttribution:
    def _request(self, bearer: str):
        return SimpleNamespace(headers={"Authorization": f"Bearer {bearer}"})

    def test_client_token_attributes_to_its_own_user(self, monkeypatch):
        from app.routes import inbox as inbox_route

        fake_user = SimpleNamespace(id=2)
        monkeypatch.setattr(
            "app.auth.client_token.resolve_token_to_user", lambda token: fake_user,
        )

        result = inbox_route._resolve_caller_user_id(self._request("sams-device-token"))

        assert result == 2

    def test_shared_inbox_token_attributes_to_legacy_owner(self, monkeypatch):
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(
            "app.auth.client_token.resolve_token_to_user", lambda token: None,
        )
        monkeypatch.setattr(
            "app.plugin.config_store.plugin_config",
            lambda name: SimpleNamespace(inbox_token="shared-tines-secret"),
        )

        result = inbox_route._resolve_caller_user_id(self._request("shared-tines-secret"))

        assert result == scan.LEGACY_OWNER_USER_ID == 1

    def test_unknown_bearer_is_unauthorized(self, monkeypatch):
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(
            "app.auth.client_token.resolve_token_to_user", lambda token: None,
        )
        monkeypatch.setattr(
            "app.plugin.config_store.plugin_config",
            lambda name: SimpleNamespace(inbox_token="shared-tines-secret"),
        )
        monkeypatch.setattr(inbox_route.settings, "ui_token", "ui-secret")

        result = inbox_route._resolve_caller_user_id(self._request("garbage"))

        assert result is None


# ---------------------------------------------------------------------------
# Task C (Tines retirement): the ingest route schedules transcription itself
# rather than waiting for the next */5 cron tick.
# ---------------------------------------------------------------------------


class TestIngestSchedulesTranscription:
    """`POST /api/inbox/ingest` fires a `BackgroundTasks` entry for
    `scan.transcribe_file_task` when — and only when — the just-ingested
    file sniffs as audio/video, and the response is unaffected either way.

    `transcribe_file_task` itself is stubbed out here (see `scheduled`
    below): it does real, billable transcription, which is exactly what
    `TestTranscribePendingIsBillable` in `test_inbox_enrichment.py` already
    pins at the unit level for `transcribe_file`. This suite is about
    *wiring* — does the route reach for it, with the right file, for the
    right kinds — not about re-proving the transcription logic itself.
    """

    @pytest.fixture
    def client(self, fake_inbox_root, fake_db, monkeypatch):
        from app.routes import inbox as inbox_route

        # Bypass bearer resolution — that's `TestIngestAttribution`'s job
        # above; this suite only cares about what happens after a caller is
        # already identified.
        monkeypatch.setattr(inbox_route, "_resolve_caller_user_id", lambda request: 1)

        app = FastAPI()
        app.include_router(inbox_route.router, prefix="/api")
        return TestClient(app)

    @pytest.fixture
    def scheduled(self, monkeypatch):
        """Stands in for `scan.transcribe_file_task` — records every path it
        was scheduled for without doing any real (async, billable) work.
        Patched on `scan` itself, matching how `InboxFacade.transcribe_in_background`
        looks it up (`scan.transcribe_file_task`, resolved at call time)."""
        calls: list = []

        async def _fake_task(path, *, prefer="openai"):
            calls.append(path)

        monkeypatch.setattr(scan, "transcribe_file_task", _fake_task)
        return calls

    def _post(self, client, filename: str, content: bytes = b"some bytes"):
        return client.post(
            "/api/inbox/ingest",
            json={"filename": filename, "data": base64.b64encode(content).decode()},
        )

    def test_audio_ingest_schedules_background_transcription(self, client, scheduled):
        response = self._post(client, "voice.m4a")

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["kind"] == "audio"
        # Scheduled exactly once, for the file this request just wrote.
        assert len(scheduled) == 1
        assert scheduled[0].name.startswith(payload["filename"].split(".")[0])

    def test_video_ingest_also_schedules_background_transcription(self, client, scheduled):
        response = self._post(client, "clip.mp4")

        assert response.status_code == 200
        assert response.json()["kind"] == "video"
        assert len(scheduled) == 1

    def test_non_audio_ingest_does_not_schedule_transcription(self, client, scheduled):
        response = self._post(client, "notes.txt", content=b"just some notes")

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["kind"] == "text"
        assert scheduled == []

    def test_response_carries_its_summary_regardless_of_scheduling(self, client, scheduled):
        """The response must not be shaped by whether a background task got
        scheduled — it returns the same `summary`/`ok` contract either way,
        with transcription happening as separate, later work."""
        response = self._post(client, "voice.m4a")
        payload = response.json()

        assert payload["ok"] is True
        assert isinstance(payload["summary"], str) and payload["summary"]
        assert len(scheduled) == 1
