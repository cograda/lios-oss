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
        # The resolver also applies the read-only scope rule, which reads the
        # method and path — ingest is a POST write.
        return SimpleNamespace(
            headers={"Authorization": f"Bearer {bearer}"},
            method="POST",
            url=SimpleNamespace(path="/api/inbox/ingest"),
        )

    def test_client_token_attributes_to_its_own_user(self, monkeypatch):
        from app.routes import inbox as inbox_route

        fake_user = SimpleNamespace(id=2)
        monkeypatch.setattr(
            "app.auth.client_token.resolve_token_to_user", lambda token: fake_user,
        )

        result = inbox_route._resolve_caller_user_id(self._request("sams-device-token"))

        assert result == 2

    def test_readonly_token_is_403_not_attributed(self, monkeypatch):
        """2026-09-07 — a `readonly` bearer authenticates but ingest is a
        write; this route bypasses `get_current_user`, so the scope rule is
        applied here by hand and must refuse before anything is written."""
        from fastapi import HTTPException

        from app.routes import inbox as inbox_route

        fake_user = SimpleNamespace(id=3, client_token_scope="readonly")
        monkeypatch.setattr(
            "app.auth.client_token.resolve_token_to_user", lambda token: fake_user,
        )

        with pytest.raises(HTTPException) as exc:
            inbox_route._resolve_caller_user_id(self._request("hall-panel-token"))
        assert exc.value.status_code == 403
        assert "read-only token" in exc.value.detail

    def test_shared_inbox_token_is_rejected(self, monkeypatch):
        """2026-09-06 — one credential: the per-user bearer. The shared
        `inbox_token` (the deleted Tines relay's webhook secret) used to be
        accepted here and attributed to user 1. Even if a stale
        `inbox_token` row is still sitting in `integration_config`, the
        route no longer consults it: an unknown bearer is a 401."""
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(
            "app.auth.client_token.resolve_token_to_user", lambda token: None,
        )
        monkeypatch.setattr(
            "app.plugin.config_store.plugin_config",
            lambda name: SimpleNamespace(inbox_token="shared-tines-secret"),
        )

        result = inbox_route._resolve_caller_user_id(self._request("shared-tines-secret"))

        assert result is None

    def test_no_shared_dashboard_secret_exists_to_ingest_with(self):
        """Same decision, other shared secret: `HOME_UI_TOKEN` used to be
        accepted here and attributed to user 1. Since PR B it does not exist
        at all — there is nothing on `settings` for the route to compare
        against."""
        from app.config import settings

        assert not hasattr(settings, "ui_token")

    def test_unknown_bearer_is_unauthorized(self, monkeypatch):
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(
            "app.auth.client_token.resolve_token_to_user", lambda token: None,
        )

        result = inbox_route._resolve_caller_user_id(self._request("garbage"))

        assert result is None

    def test_missing_bearer_gets_401_from_the_route(self, fake_inbox_root, fake_db, monkeypatch):
        monkeypatch.setattr(
            "app.auth.client_token.resolve_token_to_user", lambda token: None,
        )
        from app.routes import inbox as inbox_route

        app = FastAPI()
        app.include_router(inbox_route.router, prefix="/api")
        client = TestClient(app)

        payload = {"filename": "probe.txt", "type": "text", "data": base64.b64encode(b"hi").decode()}
        assert client.post("/api/inbox/ingest", json=payload).status_code == 401
        assert client.post(
            "/api/inbox/ingest", json=payload,
            headers={"Authorization": "Bearer shared-tines-secret"},
        ).status_code == 401

    def test_inbox_integration_owns_no_shared_webhook_secret(self):
        """The config field itself is gone — not just unread. A `secret=True`
        field the dashboard offers to set is a credential someone will set,
        and then wonder why it does nothing."""
        from app.integrations.inbox.facade import InboxFacade
        from app.integrations.inbox.manifest import MANIFEST

        assert "inbox_token" not in MANIFEST.config_schema
        assert not hasattr(InboxFacade, "default_user_id")

    def test_env_example_carries_no_inbox_token(self):
        from pathlib import Path

        env_example = Path(__file__).resolve().parents[2] / ".env.example"
        assert "HOME_INBOX_TOKEN" not in env_example.read_text()


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


# ---------------------------------------------------------------------------
# lios#198/#191 — `comar` (informally "metadata.comar") answered No must
# route the capture away from the inbox tree entirely, with no filing and
# no push. This suite is wiring, same spirit as
# `TestIngestSchedulesTranscription` above: it proves the route reaches for
# the email-only delivery path instead of the filed one, with the right
# file and the right owner — not the transcribe/email content itself, which
# is `test_inbox_enrichment.py::TestEmailOnlyCapture`'s job.
# ---------------------------------------------------------------------------


class TestMetadataComarFlag:
    @pytest.fixture
    def client(self, fake_inbox_root, fake_db, monkeypatch):
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(inbox_route, "_resolve_caller_user_id", lambda request: 1)

        app = FastAPI()
        app.include_router(inbox_route.router, prefix="/api")
        return TestClient(app)

    @pytest.fixture
    def delivered(self, monkeypatch):
        """Stands in for `scan.deliver_capture_by_email` — records every
        call without doing any real (billable, background) work. Patched on
        `scan` itself, matching how `InboxFacade.deliver_by_email_only`
        looks it up."""
        calls: list[dict] = []

        def _fake_deliver(path, *, owner_user_id, original_filename, kind):
            calls.append({
                "path": path,
                "owner_user_id": owner_user_id,
                "original_filename": original_filename,
                "kind": kind,
            })

        monkeypatch.setattr(scan, "deliver_capture_by_email", _fake_deliver)
        return calls

    @pytest.fixture
    def notified(self, monkeypatch):
        """The filed (Yes/absent) path's push + document-email attempts —
        the counterpart to `delivered` above, so a test can assert the
        email-only path never reaches either and the filed path still
        does."""
        pushes: list = []
        docs: list = []

        def _fake_notify(meta, path):
            pushes.append(path)
            return True

        def _fake_doc_email(meta, path):
            docs.append(path)
            return False

        monkeypatch.setattr(scan, "notify_ingested", _fake_notify)
        monkeypatch.setattr(scan, "email_ingested_document", _fake_doc_email)
        return {"pushes": pushes, "docs": docs}

    def _post(self, client, filename: str, *, content: bytes = b"some bytes",
              comar=None, nested: bool = False):
        payload: dict = {"filename": filename, "data": base64.b64encode(content).decode()}
        if comar is not None:
            if nested:
                payload["metadata"] = {"comar": comar}
            else:
                payload["comar"] = comar
        return client.post("/api/inbox/ingest", json=payload)

    def test_comar_false_does_not_file_audio(self, client, delivered, notified, fake_inbox_root):
        response = self._post(client, "voice.m4a", comar=False)

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["filed"] is False
        assert payload["path"] is None
        assert payload["bucket"] is None
        assert payload["kind"] == "audio"

        # Nothing landed in the inbox tree at all — no per-user dir, no
        # InboxItem row (the fake_db fixture would still be empty too, but
        # the filesystem check is the one that matters for "nothing left in
        # the inbox tree").
        assert not (fake_inbox_root / "u1").exists()

        # Delivered via the email-only path, with the right owner/kind —
        # and never through the filed path's push/document-email calls.
        assert len(delivered) == 1
        assert delivered[0]["owner_user_id"] == 1
        assert delivered[0]["kind"] == "audio"
        assert delivered[0]["original_filename"] == "voice.m4a"
        assert notified["pushes"] == []
        assert notified["docs"] == []

    @pytest.mark.parametrize("value", ["false", "False", "no", "No", "NO", "0", "off", 0, False])
    def test_liberal_falsy_parsing(self, client, delivered, value):
        response = self._post(client, "voice.m4a", comar=value)
        assert response.json()["filed"] is False
        assert len(delivered) == 1

    @pytest.mark.parametrize("value", ["yes", "Yes", "true", 1, True, "anything-else"])
    def test_truthy_and_unrecognised_values_still_file(self, client, delivered, value):
        response = self._post(client, "voice.m4a", comar=value)
        payload = response.json()
        assert payload["filed"] is True
        assert delivered == []

    def test_nested_metadata_comar_is_a_fallback(self, client, delivered):
        """The real Dictator Shortcut sends `comar` as a flat top-level key
        (see `_wants_filed`'s docstring), but a producer nesting it under
        `metadata` is honoured too."""
        response = self._post(client, "voice.m4a", comar=False, nested=True)
        assert response.json()["filed"] is False
        assert len(delivered) == 1

    def test_top_level_key_takes_precedence_over_nested(self, client, delivered):
        response = client.post(
            "/api/inbox/ingest",
            json={
                "filename": "voice.m4a",
                "data": base64.b64encode(b"x").decode(),
                "comar": False,
                "metadata": {"comar": True},
            },
        )
        assert response.json()["filed"] is False
        assert len(delivered) == 1

    def test_missing_comar_key_files_normally_and_still_notifies(self, client, delivered, notified):
        """The default (absent key) path is unchanged — including that the
        push/document-email attempt still happens. This is the "Yes path
        still notifies" regression guard for lios#198/#191."""
        response = self._post(client, "voice.m4a")
        payload = response.json()

        assert payload["ok"] is True
        assert payload["filed"] is True
        assert delivered == []
        # Audio doesn't get `email_ingested_document` (that's the
        # non-audio/text/pdf path — see `scan.email_ingested_document`), but
        # the push confirmation is attempted for every kind.
        assert len(notified["pushes"]) == 1

    def test_comar_true_files_normally_and_notifies(self, client, delivered, notified):
        response = self._post(client, "voice.m4a", comar=True)
        payload = response.json()

        assert payload["ok"] is True
        assert payload["filed"] is True
        assert delivered == []
        assert len(notified["pushes"]) == 1

    def test_non_audio_email_only_uses_the_document_path(self, client, delivered, notified):
        """A non-audio/video capture (the `send-to-comar` Shortcut's shape)
        answered No still goes through the email-only path, not the filed
        one — `deliver_capture_by_email` picks `_email_only_document` over
        `_email_only_transcribe` based on the sniffed kind, which this test
        pins at the wiring level."""
        response = self._post(client, "note.txt", content=b"hello world", comar=False)
        payload = response.json()

        assert payload["filed"] is False
        assert payload["kind"] == "text"
        assert len(delivered) == 1
        assert delivered[0]["kind"] == "text"
        assert notified["docs"] == []


# ---------------------------------------------------------------------------
# F-security: request-size guard on POST /api/inbox/ingest
# ---------------------------------------------------------------------------


class TestIngestSizeGuard:
    """`request.json()` used to buffer the WHOLE body before `MAX_DECODED_BYTES`
    was ever checked. Now a declared `Content-Length` over the ceiling is
    rejected before any read, and a missing/understated one is caught by a
    capped streamed read — see `_read_body_capped` in app/routes/inbox.py.
    """

    @pytest.fixture
    def client(self, fake_inbox_root, fake_db, monkeypatch):
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(inbox_route, "_resolve_caller_user_id", lambda request: 1)
        # Small cap so these tests don't need to move real megabytes.
        monkeypatch.setattr(inbox_route, "MAX_REQUEST_BYTES", 200)

        app = FastAPI()
        app.include_router(inbox_route.router, prefix="/api")
        return TestClient(app)

    def test_oversized_content_length_is_rejected_before_body_is_read(self, client, monkeypatch):
        from app.routes import inbox as inbox_route

        read_calls: list[int] = []
        original = inbox_route._read_body_capped

        async def _spy(request):
            read_calls.append(1)
            return await original(request)

        monkeypatch.setattr(inbox_route, "_read_body_capped", _spy)

        response = client.post(
            "/api/inbox/ingest",
            content=b"irrelevant, never read",
            headers={"Content-Length": "999999999"},
        )

        assert response.status_code == 413
        assert read_calls == [], "body must never be read once Content-Length exceeds the cap"

    def test_oversized_body_with_no_declared_length_is_still_capped(self, client):
        """A chunked request body carries no `Content-Length` at all, so the
        only thing standing between this and an unbounded read is the
        streamed cap in `_read_body_capped` — the case the pre-check above
        can't catch because there's nothing to pre-check."""
        payload = (
            b'{"filename": "big.bin", "data": "'
            + base64.b64encode(b"x" * 1000)
            + b'"}'
        )

        def _chunks():
            # Small chunks so the running total crosses the (200-byte, in
            # this test) cap mid-stream rather than in one shot.
            for i in range(0, len(payload), 16):
                yield payload[i:i + 16]

        response = client.post(
            "/api/inbox/ingest",
            content=_chunks(),
            headers={"Content-Type": "application/json"},
        )
        assert "content-length" not in {h.lower() for h in response.request.headers.keys()}
        assert response.status_code == 413

    def test_normal_payload_still_ingests(self, client):
        response = client.post(
            "/api/inbox/ingest",
            json={
                "filename": "notes.txt",
                "data": base64.b64encode(b"short note").decode(),
            },
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True


# ---------------------------------------------------------------------------
# lios#139: Cloudflare Access JWT gate wired in front of the route's own
# bearer check. `app/auth/cf_access.py` carries the unit-level coverage for
# the JWT verification itself (signature/iss/aud/exp/nbf, JWKS caching); this
# class is only about the WIRING — does the route call the gate before
# `_resolve_caller_user_id`, and does it 401 in the right shape when the gate
# says no.
# ---------------------------------------------------------------------------


class TestCloudflareAccessGate:
    @pytest.fixture
    def client(self, fake_inbox_root, fake_db, monkeypatch):
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(inbox_route, "_resolve_caller_user_id", lambda request: 1)

        app = FastAPI()
        app.include_router(inbox_route.router, prefix="/api")
        return TestClient(app)

    def _payload(self):
        return {"filename": "probe.txt", "data": base64.b64encode(b"hi").decode()}

    def test_disabled_is_a_noop_no_header_needed(self, client, monkeypatch):
        """Both settings unset (the default) — no `Cf-Access-Jwt-Assertion`
        header at all still reaches the route's own auth/ingest logic."""
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(inbox_route, "check_request", lambda headers: None)

        response = client.post("/api/inbox/ingest", json=self._payload())

        assert response.status_code == 200

    def test_gate_rejects_before_reaching_bearer_resolution(self, client, monkeypatch):
        """When the gate says no, the route must 401 WITHOUT ever calling
        `_resolve_caller_user_id` — proves this runs in front of, not
        alongside, the existing bearer check."""
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(inbox_route, "check_request", lambda headers: "Missing Cf-Access-Jwt-Assertion header")

        resolve_calls: list[int] = []
        monkeypatch.setattr(
            inbox_route, "_resolve_caller_user_id",
            lambda request: resolve_calls.append(1) or 1,
        )

        response = client.post("/api/inbox/ingest", json=self._payload())

        assert response.status_code == 401
        assert response.json()["error"] == "Missing Cf-Access-Jwt-Assertion header"
        assert resolve_calls == []

    def test_gate_passing_still_requires_the_routes_own_bearer(self, client, monkeypatch):
        """The gate is additive, not a replacement: passing it with no
        bearer must still 401 — just from `_resolve_caller_user_id`, with
        its own message, not the gate's."""
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(inbox_route, "check_request", lambda headers: None)
        monkeypatch.setattr(inbox_route, "_resolve_caller_user_id", lambda request: None)

        response = client.post("/api/inbox/ingest", json=self._payload())

        assert response.status_code == 401
        assert response.json()["error"] == "Unauthorized"

    def test_gate_and_bearer_both_passing_ingests_normally(self, client, monkeypatch):
        from app.routes import inbox as inbox_route

        monkeypatch.setattr(inbox_route, "check_request", lambda headers: None)

        response = client.post("/api/inbox/ingest", json=self._payload())

        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_end_to_end_with_real_verifier_and_settings_unset(self, client):
        """No monkeypatching of `check_request` itself here — exercises the
        real `app.auth.cf_access.check_request` via the route, with the
        settings at their default (unset) value, to prove the wiring holds
        even without stubbing the gate away."""
        response = client.post("/api/inbox/ingest", json=self._payload())

        assert response.status_code == 200
