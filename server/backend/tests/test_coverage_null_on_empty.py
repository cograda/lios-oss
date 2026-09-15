""""Honest numbers" hardening pass — coverage/percentage metrics must return
`null` on a zero denominator, never a flattering constant.

`finance_summary`'s `categorization_coverage` used to be `100.0` when
`total_count == 0` — the best possible value, presented for a period with
zero transactions (a missing import) exactly as if every transaction in it
had been categorised. Verified live: `transaction_count: 0` alongside
`categorization_coverage: 100.0`. The same `if <denominator> else <literal>`
shape existed in `lastfm_enrich`'s `coverage` (fell back to `"0%"`) and
`gmail_stats`'s `embedding_coverage` (fell back to `0`) — both fabricate a
number implying "we measured this and it's empty" when the truth is "there
was nothing to measure". All three now return `None` on a zero denominator.

db tier: `finance_summary` and `gmail_stats` build real ORM queries against
`Transaction`/`MailMessage`, so a mocked session would just test the mock.
"""

import json
from datetime import date, datetime, timezone

import pytest

from app.auth.context import use_user

pytestmark = pytest.mark.db


class TestFinanceCategorizationCoverageNull:
    def test_zero_transactions_gives_null_not_100(self, db_session):
        from app.integrations.finance.tools import handle_summary

        # A period guaranteed to have zero transactions in a freshly
        # truncated DB — no seeding needed at all.
        payload = json.loads(handle_summary(db_session, {"period": "this_month"}))
        assert payload["transaction_count"] == 0
        assert payload["categorization_coverage"] is None

    def test_nonzero_transactions_still_compute_a_real_percentage(self, db_session):
        from app.integrations.finance.models import Account, Category, Transaction
        from app.integrations.finance.tools import handle_summary

        account = Account(name="Test Current", type="AIB")
        db_session.add(account)
        db_session.flush()
        category = Category(name="Groceries")
        db_session.add(category)
        db_session.flush()

        today = date.today()
        db_session.add_all([
            Transaction(
                account_id=account.id, date=today, amount=-10.0,
                description="Categorised", category_id=category.id,
            ),
            Transaction(
                account_id=account.id, date=today, amount=-20.0,
                description="Uncategorised",
            ),
        ])
        db_session.commit()

        payload = json.loads(handle_summary(db_session, {"period": "this_month"}))
        assert payload["categorization_coverage"] == 50.0


class TestLastfmCoverageNull:
    def test_zero_artists_gives_null_not_zero_percent(self, db_session, monkeypatch):
        from app.integrations.lastfm import sync as lastfm_sync
        from app.integrations.lastfm import tools as lastfm_tools

        # handle_enrich imports enrich_artist_tags locally at call time
        # (`from app.integrations.lastfm.sync import enrich_artist_tags`),
        # so the patch target is the sync module, not tools.
        monkeypatch.setattr(lastfm_sync, "enrich_artist_tags", lambda session, limit: 0)
        with use_user(1):
            payload = json.loads(lastfm_tools.handle_enrich(db_session, {}))
        assert payload["total_artists"] == 0
        assert payload["coverage"] is None


class TestGmailEmbeddingCoverageNull:
    def test_zero_messages_gives_null_not_zero(self, db_session):
        from app.integrations.google_mail.tools import _gmail_stats_compute

        with use_user(1):
            stats = _gmail_stats_compute(db_session, {})
        assert stats["total_messages"] == 0
        assert stats["embedding_coverage"] is None

    def test_nonzero_messages_still_compute_a_real_percentage(self, db_session):
        from app.integrations.google_mail.models import MailMessage
        from app.integrations.google_mail.tools import _gmail_stats_compute
        from app.services.embedding import Embedding

        db_session.add_all([
            MailMessage(
                user_id=1, google_message_id="m1", thread_id="t1",
                account_email="a@example.com", subject="hi",
                date=datetime.now(timezone.utc),
            ),
            MailMessage(
                user_id=1, google_message_id="m2", thread_id="t2",
                account_email="a@example.com", subject="hi2",
                date=datetime.now(timezone.utc),
            ),
        ])
        db_session.commit()
        db_session.add(Embedding(
            source="email", source_id="m1", user_id=1,
            chunk_text="hi", content_hash="deadbeef",
        ))
        db_session.commit()

        with use_user(1):
            stats = _gmail_stats_compute(db_session, {})
        assert stats["total_messages"] == 2
        assert stats["embedding_coverage"] == 50.0
