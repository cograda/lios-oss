"""2026-09-06 scoping audit — regression tests.

Prompted by `/tunetasks` leaking the household task ledger to Sam (fixed in
PR #120 for `tasks`), an audit of every other per-user read found five more
places where a bound caller's request reached another user's rows:

  A. `attachments/ingest.py::ingest_one` loaded the attachment by bare id, so
     either user could publish the other's private WhatsApp document into the
     household-shared corpus.
  B. `household_capture_review` marked captures reviewed by bare id.
  C. Five scan/embed passes walked EVERY user's private messages when invoked
     from a tool — gmail's also fetching bodies with the other user's OAuth
     token. The scheduled-sync callers stay unscoped on purpose (they run with
     no bound user and attribute rows per owner); only the tool handlers bind.
  D. `apple_reminders/commands.py::expire_stale`, called from one user's SSE
     drain, expired the OTHER user's queued writes.
  E. `client_token.resolve_token_to_user` never checked `User.is_active`.

Each test here was mutation-checked: with the corresponding fix reverted, it
fails (see the PR body for the run).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.context import use_user

NOW = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _wa_document(session, *, user_id: int, message_id: str, filename: str = "boq.pdf"):
    from app.integrations.whatsapp.models import WhatsAppMessage

    raw = {"message": {"documentMessage": {
        "fileName": filename, "mimetype": "application/pdf", "fileLength": "1234",
    }}}
    row = WhatsAppMessage(
        user_id=user_id, message_id=message_id, chat_id=f"chat-{user_id}@g.us",
        chat_name="Build group", sender_id="builder", sender_name="Builder",
        is_group=True, timestamp=NOW - timedelta(days=1), message_type="document",
        media_caption=filename, is_from_me=False, raw_json=json.dumps(raw),
    )
    session.add(row)
    return row


def _wa_image(session, *, user_id: int, message_id: str):
    from app.integrations.whatsapp.models import WhatsAppMessage

    raw = {"message": {"imageMessage": {
        "mimetype": "image/jpeg", "fileLength": "4321", "caption": "snag",
    }}}
    row = WhatsAppMessage(
        user_id=user_id, message_id=message_id, chat_id=f"chat-{user_id}@g.us",
        chat_name="Build group", sender_id="builder", sender_name="Builder",
        is_group=True, timestamp=NOW - timedelta(days=1), message_type="image",
        is_from_me=False, raw_json=json.dumps(raw),
    )
    session.add(row)
    return row


def _wa_text(session, *, user_id: int, chat_id: str, body: str, when: datetime, message_id: str):
    from app.integrations.whatsapp.models import WhatsAppMessage

    session.add(WhatsAppMessage(
        user_id=user_id, message_id=message_id, chat_id=chat_id, chat_name="Chat",
        sender_id="them", sender_name="Them", is_group=False, timestamp=when,
        message_type="text", body=body, is_from_me=False,
    ))


def _attachment(session, *, user_id: int, message_ref: str = "m-1"):
    from app.integrations.attachments.models import MessageAttachment

    row = MessageAttachment(
        user_id=user_id, source="whatsapp", message_ref=message_ref,
        filename="invoice.pdf", mime_type="application/pdf", parse_status="pending",
    )
    session.add(row)
    session.commit()
    return row


def _media_item(session, *, user_id: int, message_ref: str):
    from app.integrations.media.models import MediaItem

    row = MediaItem(
        user_id=user_id, source="whatsapp", message_ref=message_ref, media_type="image",
        mime_type="image/jpeg", message_ts=NOW - timedelta(days=1), status="indexed",
    )
    session.add(row)
    return row


def _mail(session, *, user_id: int, google_message_id: str, account: str):
    from app.integrations.google_mail.models import MailMessage

    session.add(MailMessage(
        user_id=user_id, google_message_id=google_message_id, thread_id=f"t-{google_message_id}",
        account_email=account, subject="hello", sender="x@example.com",
        date=NOW - timedelta(days=1), snippet="snippet",
    ))


def _reminder_cmd(session, *, user_id: int, age_hours: int):
    from app.integrations.apple_reminders.models import ReminderCommand

    row = ReminderCommand(
        user_id=user_id, action="add", payload=json.dumps({"args": {"summary": "x"}}),
        status="pending",
    )
    session.add(row)
    session.flush()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# A. attachments ingest
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestAttachmentIngestIsOwnerScoped:
    @pytest.fixture(autouse=True)
    def _no_bridge(self, monkeypatch):
        """If the lookup lets a row through, the next step is the download —
        make that fail distinctly so the test can tell "refused at lookup"
        (`error`/`not found`) from "let through" (`failed`/`download failed`)."""
        from app.integrations.attachments import ingest as ingest_mod

        monkeypatch.setattr(
            ingest_mod, "_download",
            lambda message_ref, dest: (_ for _ in ()).throw(RuntimeError("no bridge in test")),
        )

    def test_ingest_one_refuses_another_users_attachment(self, db_session):
        from app.integrations.attachments import ingest as ingest_mod
        from app.integrations.attachments.models import MessageAttachment

        theirs = _attachment(db_session, user_id=2)
        with use_user(1):
            result = ingest_mod.ingest_one(db_session, theirs.id)

        assert result == {"id": theirs.id, "status": "error", "detail": "not found"}
        db_session.expire_all()
        assert db_session.get(MessageAttachment, theirs.id).parse_status == "pending"

    def test_foreign_id_is_indistinguishable_from_nonexistent(self, db_session):
        from app.integrations.attachments import ingest as ingest_mod

        theirs = _attachment(db_session, user_id=2)
        with use_user(1):
            foreign = ingest_mod.ingest_one(db_session, theirs.id)
            missing = ingest_mod.ingest_one(db_session, 999_999)
        assert foreign["status"] == missing["status"] == "error"
        assert foreign["detail"] == missing["detail"]

    def test_own_attachment_still_passes_the_lookup(self, db_session):
        from app.integrations.attachments import ingest as ingest_mod

        mine = _attachment(db_session, user_id=1)
        with use_user(1):
            result = ingest_mod.ingest_one(db_session, mine.id)
        assert result["status"] == "failed"
        assert "download failed" in result["detail"]

    def test_explicit_user_id_overrides_the_binding(self, db_session):
        """The unbound/scheduler shape: an explicit owner wins over (or stands
        in for) the ContextVar."""
        from app.integrations.attachments import ingest as ingest_mod

        theirs = _attachment(db_session, user_id=2)
        with use_user(1):
            result = ingest_mod.ingest_one(db_session, theirs.id, user_id=2)
        assert result["status"] == "failed"  # past the lookup

    def test_attachments_ingest_tool_uses_the_caller(self, db_session):
        from app.integrations.attachments.tools import attachments_ingest_handler

        theirs = _attachment(db_session, user_id=2)
        with use_user(1):
            out = json.loads(attachments_ingest_handler(db_session, {"ids": [theirs.id]}))
        assert out["counts"] == {"error": 1}
        assert out["results"][0]["detail"] == "not found"

    def test_gmail_row_of_another_user_is_refused_before_any_token_is_touched(self, db_session, monkeypatch):
        """Gmail became an ingestable source on 2026-09-07. The lookup gate
        is source-agnostic, but the stakes are higher here: letting the row
        through would mean fetching with the OTHER user's Google token. The
        fetcher is patched to blow up so that any leak past the lookup is
        loud and distinct from "not found"."""
        from app.integrations.attachments import ingest as ingest_mod
        from app.integrations.attachments.models import MessageAttachment

        monkeypatch.setattr(
            ingest_mod, "_download_gmail",
            lambda session, att, dest: (_ for _ in ()).throw(AssertionError("foreign gmail row reached the fetcher")),
        )
        theirs = MessageAttachment(
            user_id=2, source="gmail", message_ref="MSG_2", filename="invoice.pdf",
            mime_type="application/pdf", parse_status="pending", storage_path="ATT_2",
        )
        db_session.add(theirs)
        db_session.commit()

        with use_user(1):
            result = ingest_mod.ingest_one(db_session, theirs.id)

        assert result == {"id": theirs.id, "status": "error", "detail": "not found"}
        db_session.expire_all()
        assert db_session.get(MessageAttachment, theirs.id).parse_status == "pending"


# ---------------------------------------------------------------------------
# B. household capture review
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestCaptureReviewIsOwnerScoped:
    def _add(self, session, user_id, **overrides):
        from app.integrations.household.tools import household_capture_add_handler

        args = {"kind": "task", "capture_text": "book dentist"}
        args.update(overrides)
        with use_user(user_id):
            return json.loads(household_capture_add_handler(session, args))["created"]

    def test_review_skips_and_reports_another_users_capture(self, db_session):
        from app.integrations.household.models import HouseholdCapture
        from app.integrations.household.tools import household_capture_review_handler

        theirs = self._add(db_session, 2, capture_text="sam's item")
        with use_user(1):
            out = json.loads(household_capture_review_handler(db_session, {"ids": [theirs["id"]]}))

        assert out["reviewed"] == []
        assert out["skipped"] == [theirs["id"]]
        db_session.expire_all()
        assert db_session.get(HouseholdCapture, theirs["id"]).reviewed is False

    def test_review_of_mixed_ids_reviews_only_the_callers(self, db_session):
        from app.integrations.household.tools import household_capture_review_handler

        mine = self._add(db_session, 1, capture_text="alex's item")
        theirs = self._add(db_session, 2, capture_text="sam's item")
        with use_user(1):
            out = json.loads(
                household_capture_review_handler(db_session, {"ids": [mine["id"], theirs["id"], 999_999]})
            )
        assert out["reviewed"] == [mine["id"]]
        # Foreign and nonexistent land in the same list — no existence oracle.
        assert out["skipped"] == [theirs["id"], 999_999]
        assert out["not_found"] == out["skipped"]


# ---------------------------------------------------------------------------
# C. scan / embed passes: scoped from tools, unscoped from the scheduler
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestAttachmentScanScope:
    def test_scoped_scan_only_walks_that_users_messages(self, db_session):
        from app.integrations.attachments.models import MessageAttachment
        from app.integrations.attachments.scan import scan_whatsapp

        _wa_document(db_session, user_id=1, message_id="doc-u1")
        _wa_document(db_session, user_id=2, message_id="doc-u2")
        db_session.commit()

        result = scan_whatsapp(db_session, user_id=1)

        assert result["new_pending"] == 1
        owners = {r.user_id for r in db_session.query(MessageAttachment).all()}
        assert owners == {1}

    def test_unscoped_scan_is_household_wide(self, db_session):
        from app.integrations.attachments.models import MessageAttachment
        from app.integrations.attachments.scan import scan_whatsapp

        _wa_document(db_session, user_id=1, message_id="doc-u1")
        _wa_document(db_session, user_id=2, message_id="doc-u2")
        db_session.commit()

        scan_whatsapp(db_session)
        owners = {r.user_id for r in db_session.query(MessageAttachment).all()}
        assert owners == {1, 2}

    def test_attachments_scan_tool_passes_the_caller(self, db_session):
        from app.integrations.attachments.models import MessageAttachment
        from app.integrations.attachments import tools as att_tools

        _wa_document(db_session, user_id=1, message_id="doc-u1")
        _wa_document(db_session, user_id=2, message_id="doc-u2")
        db_session.commit()

        with use_user(2):
            att_tools.attachments_scan_handler(db_session, {})
        owners = {r.user_id for r in db_session.query(MessageAttachment).all()}
        assert owners == {2}


@pytest.mark.db
class TestMediaScanAndDownloadScope:
    def test_scoped_media_scan_only_walks_that_users_messages(self, db_session):
        from app.integrations.media.models import MediaItem
        from app.integrations.media.scan import scan_whatsapp_media

        _wa_image(db_session, user_id=1, message_id="img-u1")
        _wa_image(db_session, user_id=2, message_id="img-u2")
        db_session.commit()

        result = scan_whatsapp_media(db_session, user_id=2)

        assert result["new_indexed"] == 1
        assert {r.user_id for r in db_session.query(MediaItem).all()} == {2}

    def test_unscoped_media_scan_is_household_wide(self, db_session):
        from app.integrations.media.models import MediaItem
        from app.integrations.media.scan import scan_whatsapp_media

        _wa_image(db_session, user_id=1, message_id="img-u1")
        _wa_image(db_session, user_id=2, message_id="img-u2")
        db_session.commit()

        scan_whatsapp_media(db_session)
        assert {r.user_id for r in db_session.query(MediaItem).all()} == {1, 2}

    def test_scoped_download_pending_only_fetches_that_users_items(self, db_session, monkeypatch):
        from app.integrations.media import store

        mine = _media_item(db_session, user_id=1, message_ref="img-u1")
        _media_item(db_session, user_id=2, message_ref="img-u2")
        db_session.commit()

        fetched: list[int] = []
        monkeypatch.setattr(store, "download_item", lambda session, item: fetched.append(item.user_id) or True)

        result = store.download_pending(db_session, user_id=1)
        assert result["attempted"] == 1
        assert fetched == [1]
        assert mine.user_id == 1

    def test_unscoped_download_pending_is_household_wide(self, db_session, monkeypatch):
        from app.integrations.media import store

        _media_item(db_session, user_id=1, message_ref="img-u1")
        _media_item(db_session, user_id=2, message_ref="img-u2")
        db_session.commit()

        fetched: list[int] = []
        monkeypatch.setattr(store, "download_item", lambda session, item: fetched.append(item.user_id) or True)
        store.download_pending(db_session)
        assert sorted(fetched) == [1, 2]

    def test_media_sync_tool_passes_the_caller_to_both_steps(self, monkeypatch):
        from app.integrations.media import tools as media_tools

        seen: dict[str, int | None] = {}
        monkeypatch.setattr(
            media_tools, "scan_whatsapp_media",
            lambda session, user_id=None: seen.__setitem__("scan", user_id) or {"new_indexed": 0},
        )
        monkeypatch.setattr(
            media_tools.store, "download_pending",
            lambda session, limit=0, *, user_id=None: seen.__setitem__("download", user_id) or {"stored": 0},
        )
        with use_user(2):
            media_tools.media_sync_handler(object(), {})
        assert seen == {"scan": 2, "download": 2}


@pytest.mark.db
class TestGmailEmbedScope:
    @pytest.fixture
    def bodies_calls(self, monkeypatch):
        """Record which (account, user_id) the body fetch — i.e. the OAuth
        token — is made for, and return no bodies (snippets are used)."""
        from app.integrations.google_mail import client as gmail_client

        calls: list[tuple[str, int]] = []

        def fake_fetch(account_email, session, message_ids, *, user_id, **kw):
            calls.append((account_email, user_id))
            return {}

        monkeypatch.setattr(gmail_client, "fetch_messages_bodies", fake_fetch)
        return calls

    def test_scoped_embed_never_touches_the_other_users_token(self, db_session, bodies_calls):
        from app.integrations.embedding.models import EmbeddingQueue
        from app.integrations.google_mail.sync import embed_messages

        _mail(db_session, user_id=1, google_message_id="g-u1", account="alex@example.com")
        _mail(db_session, user_id=2, google_message_id="g-u2", account="sam@example.com")
        db_session.commit()

        count = embed_messages(db_session, user_id=1)

        assert count == 1
        assert bodies_calls == [("alex@example.com", 1)]
        queued = {q.user_id for q in db_session.query(EmbeddingQueue).filter_by(source="email").all()}
        assert queued == {1}

    def test_unscoped_embed_is_household_wide(self, db_session, bodies_calls):
        from app.integrations.google_mail.sync import embed_messages

        _mail(db_session, user_id=1, google_message_id="g-u1", account="alex@example.com")
        _mail(db_session, user_id=2, google_message_id="g-u2", account="sam@example.com")
        db_session.commit()

        assert embed_messages(db_session) == 2
        assert sorted(bodies_calls) == [("alex@example.com", 1), ("sam@example.com", 2)]

    def test_gmail_embed_tool_passes_the_caller(self, monkeypatch):
        from app.integrations.google_mail import sync as gmail_sync
        from app.integrations.google_mail import tools as gmail_tools

        seen: dict[str, int | None] = {}
        monkeypatch.setattr(
            gmail_sync, "embed_messages",
            lambda session, batch_size=200, *, user_id=None: seen.__setitem__("uid", user_id) or 0,
        )

        class _Q:
            def filter_by(self, **kw):
                return self

            def count(self):
                return 0

        class _S:
            def query(self, *a):
                return _Q()

        with use_user(2):
            out = json.loads(gmail_tools.handle_embed(_S(), {}))
        assert out["status"] == "ok"
        assert seen == {"uid": 2}


@pytest.mark.db
class TestWhatsAppEmbedScope:
    def _two_users_two_chats(self, session):
        for uid in (1, 2):
            chat = f"chat-{uid}@s.whatsapp.net"
            _wa_text(session, user_id=uid, chat_id=chat, message_id=f"u{uid}-a",
                     body="are you around for a chat later on today about the gate?", when=NOW)
            _wa_text(session, user_id=uid, chat_id=chat, message_id=f"u{uid}-b",
                     body="yeah grand, give me a shout after six and we can talk", when=NOW + timedelta(minutes=2))
        session.commit()

    def test_scoped_embed_only_cuts_that_users_chats(self, db_session, monkeypatch):
        from app.integrations.embedding.models import EmbeddingQueue
        from app.integrations.whatsapp import sync

        monkeypatch.setattr(sync, "self_chat_map", lambda: {})
        self._two_users_two_chats(db_session)

        sync.embed_messages(db_session, user_id=1)

        queued = {q.user_id for q in db_session.query(EmbeddingQueue).filter_by(source="whatsapp").all()}
        assert queued == {1}

    def test_scoped_embed_does_not_delete_the_other_users_chunks_as_stale(self, db_session, monkeypatch):
        """The trap in scoping this pass: the cleanup deletes everything absent
        from this run's keys, so a run that only walked user 1's chats would
        treat every one of user 2's chunks as stale."""
        from app.integrations.embedding.models import EmbeddingQueue
        from app.integrations.whatsapp import sync

        monkeypatch.setattr(sync, "self_chat_map", lambda: {})
        self._two_users_two_chats(db_session)

        sync.embed_messages(db_session)  # household-wide: both users queued
        u2_before = {
            q.source_id for q in
            db_session.query(EmbeddingQueue).filter_by(source="whatsapp", user_id=2).all()
        }
        assert u2_before, "fixture failed to queue anything for user 2"

        sync.embed_messages(db_session, user_id=1)

        u2_after = {
            q.source_id for q in
            db_session.query(EmbeddingQueue).filter_by(source="whatsapp", user_id=2).all()
        }
        assert u2_after == u2_before

    def test_unscoped_embed_is_household_wide(self, db_session, monkeypatch):
        from app.integrations.embedding.models import EmbeddingQueue
        from app.integrations.whatsapp import sync

        monkeypatch.setattr(sync, "self_chat_map", lambda: {})
        self._two_users_two_chats(db_session)
        sync.embed_messages(db_session)
        queued = {q.user_id for q in db_session.query(EmbeddingQueue).filter_by(source="whatsapp").all()}
        assert queued == {1, 2}

    def test_whatsapp_embed_tool_passes_the_caller(self, monkeypatch):
        from app.integrations.whatsapp import sync as wa_sync
        from app.integrations.whatsapp import tools as wa_tools

        seen: dict[str, int | None] = {}
        monkeypatch.setattr(
            wa_sync, "embed_messages",
            lambda session, batch_size=200, *, user_id=None: seen.__setitem__("uid", user_id) or 0,
        )

        class _Q:
            def filter_by(self, **kw):
                return self

            def count(self):
                return 0

        class _S:
            def query(self, *a):
                return _Q()

        with use_user(2):
            out = json.loads(wa_tools.handle_embed(_S(), {}))
        assert out["status"] == "ok"
        assert seen == {"uid": 2}


# ---------------------------------------------------------------------------
# D. reminders expire_stale from the per-user drain
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestExpireStaleScope:
    def test_scoped_expire_leaves_the_other_users_rows_pending(self, db_session):
        from app.integrations.apple_reminders import commands

        mine = _reminder_cmd(db_session, user_id=1, age_hours=5)
        theirs = _reminder_cmd(db_session, user_id=2, age_hours=5)

        n = commands.expire_stale(db_session, user_id=1)

        db_session.refresh(mine)
        db_session.refresh(theirs)
        assert n == 1
        assert mine.status == "expired"
        assert theirs.status == "pending"

    def test_unscoped_expire_is_household_wide(self, db_session):
        from app.integrations.apple_reminders import commands

        _reminder_cmd(db_session, user_id=1, age_hours=5)
        _reminder_cmd(db_session, user_id=2, age_hours=5)
        assert commands.expire_stale(db_session) == 2

    def test_one_daemons_drain_does_not_expire_the_other_users_writes(self, db_session, monkeypatch):
        from app.integrations.apple_reminders import commands

        theirs = _reminder_cmd(db_session, user_id=2, age_hours=5)
        monkeypatch.setattr(commands.stream_manager, "loop", None)

        counts = commands.drain_pending(db_session, user_id=1, user_name="alex")

        db_session.refresh(theirs)
        assert counts["expired"] == 0
        assert theirs.status == "pending"


# ---------------------------------------------------------------------------
# E. bearer of a deactivated user
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestInactiveUserBearerIsRefused:
    def _user_with_token(self, session, *, is_active: bool) -> str:
        from app.models.clients import ClientToken
        from app.models.users import User

        user = User(name=f"ghost-{int(is_active)}", display_name="Ghost", is_active=is_active)
        session.add(user)
        session.flush()
        token = f"audit-token-{user.id}"
        session.add(ClientToken.for_token(user_id=user.id, token=token, label="test"))
        session.commit()
        return token

    def test_inactive_users_token_resolves_to_none(self, db_session):
        from app.auth.client_token import resolve_token_to_user

        token = self._user_with_token(db_session, is_active=False)
        assert resolve_token_to_user(token) is None

    def test_active_users_token_still_resolves(self, db_session):
        from app.auth.client_token import resolve_token_to_user

        token = self._user_with_token(db_session, is_active=True)
        user = resolve_token_to_user(token)
        assert user is not None and user.name == "ghost-1"
