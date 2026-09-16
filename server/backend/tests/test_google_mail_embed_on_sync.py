"""New mail flows into the embedding queue continuously, keyed by
`google_message_id` (S5, 2026-09-07) — companion to `tasks/intake.py`'s
WhatsApp segment-matching fix. Before this, nothing ever enqueued mail for
embedding except the manual `gmail_embed` tool, so `google_mail`'s intake
matching (keyed by `google_message_id`, already correct) could never find a
vector to match against.

`GoogleMailIntegration.store()` now calls `sync.enqueue_new_mail` right after
`store_mail` upserts — the mail-side mirror of `WhatsAppIntegration.sync()`
calling `whatsapp.sync.embed_messages` after every WhatsApp sync.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)


def _mail(
    session, *, user_id: int, google_message_id: str, account: str = "alex@example.com",
    date: datetime | None = None,
):
    from app.integrations.google_mail.models import MailMessage

    session.add(MailMessage(
        user_id=user_id, google_message_id=google_message_id,
        thread_id=f"t-{google_message_id}", account_email=account,
        subject="hello", sender="x@example.com", date=date or (NOW - timedelta(days=1)),
        snippet="snippet",
    ))


def _legacy_tm_row(session, *, user_id: int, sha: str = "deadbeef"):
    """A historical `email`-sourced import row keyed `tm:<sha256>`, exactly
    the shape the August bulk import left behind — this codebase never
    wrote it and must never touch it (see `sync.py::enqueue_new_mail`'s
    docstring)."""
    from app.services.embedding import Embedding

    row = Embedding(
        source="email", source_id=f"tm:{sha}", user_id=user_id,
        chunk_text="legacy imported mail", content_hash=f"hash-{sha}",
    )
    session.add(row)
    return row


@pytest.fixture
def bodies_calls(monkeypatch):
    from app.integrations.google_mail import client as gmail_client

    calls: list[tuple[str, int]] = []

    def fake_fetch(account_email, session, message_ids, *, user_id, **kw):
        calls.append((account_email, user_id))
        return {}

    monkeypatch.setattr(gmail_client, "fetch_messages_bodies", fake_fetch)
    return calls


class TestEnqueueNewMail:
    def test_freshly_synced_mail_is_enqueued(self, db_session, bodies_calls):
        from app.integrations.embedding.models import EmbeddingQueue
        from app.integrations.google_mail.sync import enqueue_new_mail

        _mail(db_session, user_id=1, google_message_id="g-new")
        db_session.commit()

        count = enqueue_new_mail(db_session, ["g-new"], user_id=1)

        assert count == 1
        assert bodies_calls == [("alex@example.com", 1)]
        queued = db_session.query(EmbeddingQueue).filter_by(source="email", source_id="g-new").one()
        assert queued.user_id == 1

    def test_already_embedded_message_is_not_re_fetched(self, db_session, bodies_calls):
        """A routine metadata-only re-sync (e.g. a read/starred flip) must
        not re-fetch the body of a message already embedded."""
        from app.integrations.google_mail.sync import enqueue_new_mail
        from app.services.embedding import Embedding

        _mail(db_session, user_id=1, google_message_id="g-old")
        db_session.add(Embedding(
            source="email", source_id="g-old", user_id=1,
            chunk_text="already embedded", content_hash="h1",
        ))
        db_session.commit()

        count = enqueue_new_mail(db_session, ["g-old"], user_id=1)

        assert count == 0
        assert bodies_calls == []

    def test_never_touches_legacy_tm_rows(self, db_session, bodies_calls):
        """The supersede/dedup check is keyed on (source, source_id,
        user_id); the legacy `tm:<sha256>` rows carry a different
        source_id entirely, so they must survive untouched — and must
        never be treated as 'already embedded' for a real
        google_message_id (mutation-check: an id-shape-blind anti-join
        would wrongly skip a genuinely new message if any tm: row existed
        for that user)."""
        from app.integrations.google_mail.sync import enqueue_new_mail
        from app.services.embedding import Embedding

        _mail(db_session, user_id=1, google_message_id="g-brand-new")
        _legacy_tm_row(db_session, user_id=1)
        db_session.commit()

        before_ids = {
            r.source_id for r in db_session.query(Embedding).filter_by(source="email", user_id=1)
        }
        assert before_ids == {"tm:deadbeef"}

        count = enqueue_new_mail(db_session, ["g-brand-new"], user_id=1)

        assert count == 1  # the tm: row didn't shadow the real message
        after_ids = {
            r.source_id for r in db_session.query(Embedding).filter_by(source="email", user_id=1)
        }
        assert after_ids == {"tm:deadbeef"}  # untouched — nothing deleted it

    def test_scoped_to_the_owning_user(self, db_session, bodies_calls):
        from app.integrations.google_mail.sync import enqueue_new_mail

        _mail(db_session, user_id=1, google_message_id="g-u1", account="alex@example.com")
        _mail(db_session, user_id=2, google_message_id="g-u2", account="sam@example.com")
        db_session.commit()

        count = enqueue_new_mail(db_session, ["g-u1", "g-u2"], user_id=1)

        assert count == 1
        assert bodies_calls == [("alex@example.com", 1)]

    def test_empty_ids_is_a_clean_no_op(self, db_session, bodies_calls):
        from app.integrations.google_mail.sync import enqueue_new_mail

        assert enqueue_new_mail(db_session, [], user_id=1) == 0
        assert bodies_calls == []


class TestStoreHooksEmbedding:
    def test_store_enqueues_the_records_it_just_upserted(self, db_session, bodies_calls):
        from app.integrations.google_mail import GoogleMailIntegration
        from app.integrations.embedding.models import EmbeddingQueue

        records = [{
            "google_message_id": "g-store-1", "thread_id": "t1",
            "account_email": "alex@example.com", "user_id": 1,
            "subject": "hi", "sender": "x@example.com", "to": "alex@example.com",
            "date": NOW.isoformat(), "snippet": "hello there",
            "labels": "INBOX", "is_read": False, "is_starred": False,
            "has_attachments": False, "size_estimate": 100,
        }]

        integration = GoogleMailIntegration()
        count = integration.store(db_session, records)

        assert count == 1
        assert bodies_calls == [("alex@example.com", 1)]
        queued = db_session.query(EmbeddingQueue).filter_by(source="email", source_id="g-store-1").one()
        assert queued.user_id == 1

    def test_store_with_no_records_enqueues_nothing(self, db_session, bodies_calls):
        from app.integrations.google_mail import GoogleMailIntegration

        integration = GoogleMailIntegration()
        assert integration.store(db_session, []) == 0
        assert bodies_calls == []


class TestEmbedMessagesUnchanged:
    """`embed_messages` (the `gmail_embed` manual-backfill path) keeps its
    existing scoping behaviour — this refactor only factored its per-batch
    body-fetch-and-enqueue into `_enqueue_mail_batch`, shared with
    `enqueue_new_mail`; it must not change what gets enqueued."""

    def test_backfill_still_finds_unembedded_mail(self, db_session, bodies_calls):
        from app.integrations.google_mail.sync import embed_messages

        _mail(db_session, user_id=1, google_message_id="g-backfill")
        db_session.commit()

        assert embed_messages(db_session, user_id=1) == 1
        assert bodies_calls == [("alex@example.com", 1)]

    def test_backfill_does_not_touch_legacy_tm_rows(self, db_session, bodies_calls):
        from app.integrations.google_mail.sync import embed_messages
        from app.services.embedding import Embedding

        _mail(db_session, user_id=1, google_message_id="g-backfill-2")
        _legacy_tm_row(db_session, user_id=1, sha="cafef00d")
        db_session.commit()

        embed_messages(db_session, user_id=1)

        remaining = {
            r.source_id for r in db_session.query(Embedding).filter_by(source="email", user_id=1)
            if r.source_id.startswith("tm:")
        }
        assert remaining == {"tm:cafef00d"}


# ─── S5.1: bounded backfill (embed_backfill_from + max_messages) ───────────


class TestBoundedBackfill:
    def test_mail_before_the_backfill_floor_is_excluded(self, db_session, bodies_calls):
        """Default floor is 2026-07-15 — mail from before it (the era the
        historical timemachine import already covers) must not be enqueued."""
        from app.integrations.google_mail.sync import embed_messages

        _mail(db_session, user_id=1, google_message_id="g-old-2020", date=datetime(2020, 1, 1, tzinfo=timezone.utc))
        _mail(db_session, user_id=1, google_message_id="g-new-2026", date=datetime(2026, 8, 1, tzinfo=timezone.utc))
        db_session.commit()

        assert embed_messages(db_session, user_id=1) == 1
        assert bodies_calls == [("alex@example.com", 1)]

    def test_null_dated_mail_is_excluded_not_included(self, db_session, bodies_calls):
        """A message with no reliable date can't be compared to the floor —
        it must be treated as out of scope, not swept in by default
        (mutation-check: an `is_(None)`-inclusive filter would enqueue it)."""
        from app.integrations.google_mail.models import MailMessage
        from app.integrations.google_mail.sync import embed_messages

        db_session.add(MailMessage(
            user_id=1, google_message_id="g-no-date", thread_id="t1",
            account_email="alex@example.com", subject="hi", sender="x@example.com",
            date=None, snippet="snippet",
        ))
        db_session.commit()

        assert embed_messages(db_session, user_id=1) == 0
        assert bodies_calls == []

    def test_config_key_moves_the_floor(self, db_session, bodies_calls):
        from app.plugin import config_store
        from app.integrations.google_mail.sync import embed_messages

        config_store.set_config_value("google_mail", "embed_backfill_from", "2026-09-01")

        _mail(db_session, user_id=1, google_message_id="g-before-custom-floor", date=datetime(2026, 8, 20, tzinfo=timezone.utc))
        _mail(db_session, user_id=1, google_message_id="g-after-custom-floor", date=datetime(2026, 9, 3, tzinfo=timezone.utc))
        db_session.commit()

        assert embed_messages(db_session, user_id=1) == 1
        assert bodies_calls == [("alex@example.com", 1)]

    def test_dry_run_plan_matches_what_a_real_run_would_enqueue(self, db_session, bodies_calls):
        from app.integrations.google_mail.sync import embed_messages, plan_embed_messages

        _mail(db_session, user_id=1, google_message_id="g-plan-1", date=datetime(2026, 8, 1, tzinfo=timezone.utc))
        _mail(db_session, user_id=1, google_message_id="g-plan-2", date=datetime(2026, 8, 15, tzinfo=timezone.utc))
        _mail(db_session, user_id=1, google_message_id="g-plan-too-old", date=datetime(2020, 1, 1, tzinfo=timezone.utc))
        db_session.commit()

        plan = plan_embed_messages(db_session, user_id=1)
        assert plan["count"] == 2
        assert plan["earliest"].startswith("2026-08-01")
        assert plan["latest"].startswith("2026-08-15")
        assert bodies_calls == []  # dry run must never fetch a body

        # And the real run enqueues exactly the count the plan reported.
        assert embed_messages(db_session, user_id=1) == plan["count"]

    def test_refuses_rather_than_truncates_over_max_messages(self, db_session, bodies_calls):
        from app.integrations.google_mail.sync import EmbedBacklogTooLargeError, embed_messages
        from app.integrations.embedding.models import EmbeddingQueue

        for i in range(5):
            _mail(db_session, user_id=1, google_message_id=f"g-cap-{i}", date=datetime(2026, 8, 1, tzinfo=timezone.utc))
        db_session.commit()

        with pytest.raises(EmbedBacklogTooLargeError, match="5"):
            embed_messages(db_session, user_id=1, max_messages=3)

        # Refusal must be all-or-nothing — nothing partially enqueued.
        assert db_session.query(EmbeddingQueue).filter_by(source="email").count() == 0
        assert bodies_calls == []

    def test_max_messages_none_disables_the_cap(self, db_session, bodies_calls):
        from app.integrations.google_mail.sync import embed_messages

        for i in range(5):
            _mail(db_session, user_id=1, google_message_id=f"g-nocap-{i}", date=datetime(2026, 8, 1, tzinfo=timezone.utc))
        db_session.commit()

        assert embed_messages(db_session, user_id=1, max_messages=None) == 5

    def test_gmail_embed_tool_dry_run_reports_without_enqueuing(self, db_session, bodies_calls):
        from app.auth.context import use_user
        from app.integrations.google_mail import tools as gmail_tools
        from app.integrations.embedding.models import EmbeddingQueue

        _mail(db_session, user_id=1, google_message_id="g-tool-dry", date=datetime(2026, 8, 1, tzinfo=timezone.utc))
        db_session.commit()

        with use_user(1):
            out = json.loads(gmail_tools.handle_embed(db_session, {"dry_run": True}))

        assert out["status"] == "ok"
        assert out["dry_run"] is True
        assert out["count"] == 1
        assert db_session.query(EmbeddingQueue).filter_by(source="email").count() == 0
        assert bodies_calls == []

    def test_gmail_embed_tool_refusal_surfaces_as_an_error_not_a_crash(self, db_session, monkeypatch):
        from app.auth.context import use_user
        from app.integrations.google_mail import tools as gmail_tools
        from app.integrations.google_mail import sync as gmail_sync

        def fake_embed(session, batch_size=200, *, user_id=None, max_messages=5000):
            raise gmail_sync.EmbedBacklogTooLargeError("would enqueue 9001 messages, over the max_messages cap of 5000")

        monkeypatch.setattr(gmail_sync, "embed_messages", fake_embed)

        with use_user(1):
            out = json.loads(gmail_tools.handle_embed(db_session, {}))

        assert "error" in out
        assert "9001" in out["error"]


# ─── S5.1: embed_messages' own anti-join must not cross users ─────────────


class TestEmbedMessagesNeverCrossesUsers:
    def test_one_users_embedding_never_suppresses_the_others_same_id(self, db_session, bodies_calls):
        """Two users can (in theory — google_message_id is unique only per
        Gmail account, per MailMessage's own docstring) hold the same
        google_message_id. User 2 embedding theirs must not make user 1's
        read as already-embedded (mutation-check: a flat cross-user
        `NOT IN` anti-join, as `embed_messages` used before S5.1, fails
        this by construction)."""
        from app.integrations.google_mail.sync import embed_messages
        from app.services.embedding import Embedding

        shared_id = "g-shared-across-users"
        _mail(db_session, user_id=1, google_message_id=shared_id, account="alex@example.com", date=datetime(2026, 8, 1, tzinfo=timezone.utc))
        _mail(db_session, user_id=2, google_message_id=shared_id, account="sam@example.com", date=datetime(2026, 8, 1, tzinfo=timezone.utc))
        db_session.add(Embedding(
            source="email", source_id=shared_id, user_id=2,
            chunk_text="sam's copy, already embedded", content_hash="h-sam",
        ))
        db_session.commit()

        # User 1's identically-keyed message must still be found and enqueued.
        assert embed_messages(db_session, user_id=1) == 1
        assert bodies_calls == [("alex@example.com", 1)]
