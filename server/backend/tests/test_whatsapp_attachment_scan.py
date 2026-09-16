"""WhatsApp attachment discovery is per user, not per message id (2026-09-07).

With two bridges, a document posted to a chat both people are in exists as
two `whatsapp_messages` rows — same `message_id`, different `user_id`. The
anti-join in `scan_whatsapp` used to match on `message_ref` alone, so the
attachment was "owned by whoever scanned first" and the other user's row was
never discovered (their `attachments_pending` simply did not show it). The
unique constraint was already `(user_id, source, message_ref, filename)`;
the join now asks the same question.

db tier: real Postgres, two seeded users (conftest `_seed_users`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.integrations.attachments.models import MessageAttachment
from app.integrations.attachments.scan import scan_whatsapp
from app.integrations.whatsapp.models import WhatsAppMessage

SHARED_ID = "3EB0SHAREDDOC"


def _document_row(user_id: int, message_id: str = SHARED_ID) -> WhatsAppMessage:
    raw = {
        "message": {
            "documentMessage": {
                "fileName": "boiler-service.pdf",
                "mimetype": "application/pdf",
                "fileLength": "12345",
            }
        }
    }
    return WhatsAppMessage(
        user_id=user_id,
        message_id=message_id,
        chat_id="12345-678@g.us",
        chat_name="Family",
        sender_id="353870000000@s.whatsapp.net",
        sender_name="Someone",
        is_group=True,
        timestamp=datetime.now(timezone.utc),
        message_type="document",
        media_caption="boiler-service.pdf",
        is_from_me=False,
        raw_json=json.dumps(raw),
    )


def _rows_for(session, message_ref: str) -> list[MessageAttachment]:
    return (
        session.query(MessageAttachment)
        .filter_by(source="whatsapp", message_ref=message_ref)
        .order_by(MessageAttachment.user_id)
        .all()
    )


@pytest.mark.db
class TestPerUserDiscovery:
    def test_same_message_held_by_two_users_yields_one_row_each(self, db_session):
        db_session.add_all([_document_row(1), _document_row(2)])
        db_session.commit()

        first = scan_whatsapp(db_session, user_id=1)
        second = scan_whatsapp(db_session, user_id=2)
        assert first["new_pending"] == 1
        # The bug: this was 0 — user 2's row matched user 1's attachment.
        assert second["new_pending"] == 1

        rows = _rows_for(db_session, SHARED_ID)
        assert [r.user_id for r in rows] == [1, 2]
        assert all(r.filename == "boiler-service.pdf" for r in rows)

    def test_household_scan_discovers_both_in_one_pass(self, db_session):
        db_session.add_all([_document_row(1), _document_row(2)])
        db_session.commit()

        result = scan_whatsapp(db_session)  # scheduled-sync shape: no bound user
        assert result["new_pending"] == 2
        assert [r.user_id for r in _rows_for(db_session, SHARED_ID)] == [1, 2]

    def test_rescan_is_still_idempotent_per_user(self, db_session):
        db_session.add_all([_document_row(1), _document_row(2)])
        db_session.commit()
        scan_whatsapp(db_session)

        again = scan_whatsapp(db_session)
        assert again["new_pending"] == 0
        assert len(_rows_for(db_session, SHARED_ID)) == 2

    def test_scoped_scan_never_touches_the_other_users_messages(self, db_session):
        db_session.add_all([_document_row(1), _document_row(2, message_id="ONLY-USER-2")])
        db_session.commit()

        scan_whatsapp(db_session, user_id=1)
        assert [r.user_id for r in _rows_for(db_session, SHARED_ID)] == [1]
        assert _rows_for(db_session, "ONLY-USER-2") == []
