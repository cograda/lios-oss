"""Date-range filtering for the semantic search tools (db tier).

`search_semantic`, `gmail_semantic_search`, and `whatsapp_semantic_search`
took only `query`/`limit` before this — a retrieval measurement on
2026-08-13 found that same-topic-different-year hits pollute results (a
question about this week's washing machine returned a semantically similar
email from 2012). This file pins the fix: optional `after`/`before` args
(matching `ListTool`'s naming and semantics) that narrow results to the
source's *real* date — the mail's sent date / the WhatsApp segment's start
timestamp — not `Embedding.created_at` (embedding/ingestion time, which for
backfilled history has nothing to do with when the content was created).

Every seeded embedding row shares the same fixed vector (`FIXED_QUERY_VEC`,
mirroring `test_tool_snapshots.py`'s `stub_embedding_model` pattern) so
cosine distance is always exactly 0 and score is always 1.0 — the date
filter, not similarity ranking, is what's under test.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.context import use_user

pytestmark = pytest.mark.db

FIXED_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
OLD_DATE = datetime(2012, 3, 1, 9, 0, 0, tzinfo=timezone.utc)

VECTOR_DIM = 384


def _one_hot(index: int) -> list[float]:
    v = [0.0] * VECTOR_DIM
    v[index] = 1.0
    return v


FIXED_QUERY_VEC = _one_hot(0)


@pytest.fixture
def stub_embedding_model(monkeypatch):
    """Deterministic query embedding — see test_tool_snapshots.py's fixture
    of the same name, duplicated here to keep this file independent."""
    import numpy as np

    from app.services import embedding as emb_mod

    class _FakeModel:
        def embed(self, texts):
            return [np.array(FIXED_QUERY_VEC, dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb_mod, "get_model", lambda: _FakeModel())


def _seed_embedding(session, *, source, source_id, user_id, chunk_text, metadata=None, created_at=None):
    from app.services.embedding import Embedding, EmbeddingVecBgeSmall384, MODEL_NAME

    row = Embedding(
        source=source,
        source_id=source_id,
        user_id=user_id,
        chunk_index=0,
        chunk_text=chunk_text,
        content_hash=f"hash-{source}-{source_id}",
        metadata_json=json.dumps(metadata) if metadata is not None else None,
        created_at=created_at or FIXED_NOW,
    )
    session.add(row)
    session.flush()
    session.add(EmbeddingVecBgeSmall384(
        embedding_id=row.id,
        embedding=FIXED_QUERY_VEC,
        model_name=MODEL_NAME,
    ))
    return row


# ---------------------------------------------------------------------------
# gmail_semantic_search
# ---------------------------------------------------------------------------

class TestGmailSemanticSearchDateFilter:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.google_mail.models import MailMessage

        db_session.add_all([
            MailMessage(
                user_id=1, google_message_id="old-mail", thread_id="t1",
                account_email="alex@example.com", subject="Washing machine drum bearing",
                sender="repair@example.com", to="alex@example.com",
                date=OLD_DATE, snippet="old", labels="INBOX",
                is_read=True, is_starred=False, has_attachments=False,
                size_estimate=100, synced_at=OLD_DATE,
            ),
            MailMessage(
                user_id=1, google_message_id="new-mail", thread_id="t2",
                account_email="alex@example.com", subject="Washing machine service due",
                sender="repair@example.com", to="alex@example.com",
                date=FIXED_NOW, snippet="new", labels="INBOX",
                is_read=True, is_starred=False, has_attachments=False,
                size_estimate=100, synced_at=FIXED_NOW,
            ),
        ])
        db_session.flush()

        _seed_embedding(db_session, source="email", source_id="old-mail", user_id=1,
                         chunk_text="Washing machine drum bearing replacement 2012")
        _seed_embedding(db_session, source="email", source_id="new-mail", user_id=1,
                         chunk_text="Washing machine service due this week")
        db_session.commit()

    def _call(self, db_session, arguments):
        from app.integrations.google_mail.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "gmail_semantic_search")
        with use_user(1):
            return json.loads(tool["handler"](db_session, arguments))

    def test_no_filter_returns_both_unchanged(self, db_session, stub_embedding_model):
        """Omitting after/before must preserve today's behaviour exactly —
        both messages come back, regardless of the 14-year gap between them."""
        results = self._call(db_session, {"query": "washing machine"})
        ids = {r["id"] for r in results}
        assert ids == {"old-mail", "new-mail"}

    def test_after_narrows_to_recent(self, db_session, stub_embedding_model):
        results = self._call(db_session, {"query": "washing machine", "after": "2020-01-01"})
        ids = {r["id"] for r in results}
        assert ids == {"new-mail"}

    def test_before_narrows_to_old(self, db_session, stub_embedding_model):
        results = self._call(db_session, {"query": "washing machine", "before": "2020-01-01"})
        ids = {r["id"] for r in results}
        assert ids == {"old-mail"}

    def test_after_and_before_bound_a_window(self, db_session, stub_embedding_model):
        results = self._call(db_session, {
            "query": "washing machine",
            "after": "2026-01-01",
            "before": "2026-12-31",
        })
        ids = {r["id"] for r in results}
        assert ids == {"new-mail"}

    def test_malformed_date_is_ignored_not_an_error(self, db_session, stub_embedding_model):
        """Matches ListTool's `parse_iso_date` behaviour: an unparseable date
        string is silently treated as "no filter", not a validation error."""
        results = self._call(db_session, {"query": "washing machine", "after": "not-a-date"})
        ids = {r["id"] for r in results}
        assert ids == {"old-mail", "new-mail"}

    def test_range_excluding_everything_reports_why(self, db_session, stub_embedding_model):
        """An empty result caused by the date filter should say so, rather
        than silently returning `[]` indistinguishable from 'no matches'."""
        out = self._call(db_session, {"query": "washing machine", "after": "2099-01-01"})
        # A filter matching nothing is not an error - see the note in
        # embedding/tools.py. Empty results plus an explanatory note.
        assert "error" not in out
        assert out["results"] == []
        assert "date range" in out["note"]


# ---------------------------------------------------------------------------
# whatsapp_semantic_search
# ---------------------------------------------------------------------------

class TestWhatsappSemanticSearchDateFilter:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        _seed_embedding(db_session, source="whatsapp", source_id="segment-old", user_id=1,
                         chunk_text="Sam: the washing machine is making a noise again",
                         metadata={
                             "chat_id": "chat1", "chat_name": "Sam", "is_group": False,
                             "start": OLD_DATE.isoformat(), "end": OLD_DATE.isoformat(),
                             "message_count": 1, "participants": ["Sam"],
                         })
        _seed_embedding(db_session, source="whatsapp", source_id="segment-new", user_id=1,
                         chunk_text="Sam: washing machine repair booked for Thursday",
                         metadata={
                             "chat_id": "chat1", "chat_name": "Sam", "is_group": False,
                             "start": FIXED_NOW.isoformat(), "end": FIXED_NOW.isoformat(),
                             "message_count": 1, "participants": ["Sam"],
                         })
        db_session.commit()

    def _call(self, db_session, arguments):
        from app.integrations.whatsapp.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "whatsapp_semantic_search")
        with use_user(1):
            return json.loads(tool["handler"](db_session, arguments))

    def test_no_filter_returns_both_unchanged(self, db_session, stub_embedding_model):
        results = self._call(db_session, {"query": "washing machine"})
        ids = {r["segment_id"] for r in results}
        assert ids == {"segment-old", "segment-new"}

    def test_after_narrows_to_recent(self, db_session, stub_embedding_model):
        results = self._call(db_session, {"query": "washing machine", "after": "2020-01-01"})
        ids = {r["segment_id"] for r in results}
        assert ids == {"segment-new"}

    def test_before_narrows_to_old(self, db_session, stub_embedding_model):
        results = self._call(db_session, {"query": "washing machine", "before": "2020-01-01"})
        ids = {r["segment_id"] for r in results}
        assert ids == {"segment-old"}

    def test_malformed_date_is_ignored_not_an_error(self, db_session, stub_embedding_model):
        results = self._call(db_session, {"query": "washing machine", "before": "banana"})
        ids = {r["segment_id"] for r in results}
        assert ids == {"segment-old", "segment-new"}

    def test_range_excluding_everything_reports_why(self, db_session, stub_embedding_model):
        out = self._call(db_session, {"query": "washing machine", "before": "1999-01-01"})
        # A filter matching nothing is not an error - see the note in
        # embedding/tools.py. Empty results plus an explanatory note.
        assert "error" not in out
        assert out["results"] == []
        assert "date range" in out["note"]


# ---------------------------------------------------------------------------
# search_semantic (cross-source)
# ---------------------------------------------------------------------------

class TestCrossSourceSemanticSearchDateFilter:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.google_mail.models import MailMessage

        db_session.add(MailMessage(
            user_id=1, google_message_id="old-mail", thread_id="t1",
            account_email="alex@example.com", subject="Washing machine drum bearing",
            sender="repair@example.com", to="alex@example.com",
            date=OLD_DATE, snippet="old", labels="INBOX",
            is_read=True, is_starred=False, has_attachments=False,
            size_estimate=100, synced_at=OLD_DATE,
        ))
        db_session.flush()

        _seed_embedding(db_session, source="email", source_id="old-mail", user_id=1,
                         chunk_text="Washing machine drum bearing replacement 2012")
        # Household-shared vault source has no per-item date metadata at all —
        # it should pass through a date filter unaffected (documented, not silent).
        _seed_embedding(db_session, source="vault", source_id="Household/Appliances.md", user_id=None,
                         chunk_text="Washing machine model number and manual location")
        db_session.commit()

    def _call(self, db_session, arguments):
        from app.integrations.embedding.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "search_semantic")
        with use_user(1):
            return json.loads(tool["handler"](db_session, arguments))

    def test_no_filter_returns_everything_unchanged(self, db_session, stub_embedding_model):
        results = self._call(db_session, {"query": "washing machine"})
        source_ids = {r["source_id"] for r in results}
        assert source_ids == {"old-mail", "Household/Appliances.md"}

    def test_after_filter_excludes_old_mail_but_not_vault(self, db_session, stub_embedding_model):
        """The date-aware source (email) is narrowed; the source with no
        reliable per-item date (vault) is deliberately left unaffected —
        see `_build_cross_source_date_filter`'s docstring."""
        results = self._call(db_session, {"query": "washing machine", "after": "2020-01-01"})
        source_ids = {r["source_id"] for r in results}
        assert source_ids == {"Household/Appliances.md"}

    def test_malformed_date_is_ignored_not_an_error(self, db_session, stub_embedding_model):
        results = self._call(db_session, {"query": "washing machine", "after": "not-a-date"})
        source_ids = {r["source_id"] for r in results}
        assert source_ids == {"old-mail", "Household/Appliances.md"}
