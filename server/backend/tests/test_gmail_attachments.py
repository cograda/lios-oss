"""Gmail attachment scanning tests.

Unit tier: `_walk_attachment_parts` (pure MIME-tree walker, no I/O) against a
nested multipart fixture.

db tier: `scan_gmail` / `_scan_gmail_account` against real Postgres, with the
Gmail API boundary (`list_attachment_candidates_page`, `fetch_messages_full`)
monkeypatched — exercises candidate selection, idempotency (re-scan doesn't
duplicate rows or re-fetch already-recorded messages), the checkpoint cursor,
and per-user account scoping.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.integrations.google_mail.client import _walk_attachment_parts


# ---------------------------------------------------------------------------
# Unit tier — pure parts-walking
# ---------------------------------------------------------------------------

def _attachment_part(filename: str, mime_type: str, attachment_id: str, size: int = 100) -> dict:
    return {
        "filename": filename,
        "mimeType": mime_type,
        "body": {"attachmentId": attachment_id, "size": size},
    }


def _inline_part(mime_type: str = "text/plain") -> dict:
    return {"filename": "", "mimeType": mime_type, "body": {"size": 42, "data": "abc"}}


class TestWalkAttachmentParts:
    def test_flat_multipart_with_one_attachment(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                _inline_part("text/plain"),
                _attachment_part("invoice.pdf", "application/pdf", "ATT_1"),
            ],
        }
        found = _walk_attachment_parts(payload)
        assert len(found) == 1
        assert found[0] == {
            "filename": "invoice.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 100,
            "attachment_id": "ATT_1",
        }

    def test_nested_multipart_alternative_plus_attachment(self):
        # Typical real-world shape: multipart/mixed > [multipart/alternative
        # > [text/plain, text/html], application/pdf attachment].
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        _inline_part("text/plain"),
                        _inline_part("text/html"),
                    ],
                },
                _attachment_part("contract.docx",
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                  "ATT_2"),
            ],
        }
        found = _walk_attachment_parts(payload)
        assert len(found) == 1
        assert found[0]["filename"] == "contract.docx"
        assert found[0]["attachment_id"] == "ATT_2"

    def test_multiple_attachments_at_different_depths(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                _attachment_part("top.pdf", "application/pdf", "ATT_TOP"),
                {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        _attachment_part("nested.xlsx",
                                          "application/vnd.ms-excel", "ATT_NESTED"),
                    ],
                },
            ],
        }
        found = _walk_attachment_parts(payload)
        filenames = {a["filename"] for a in found}
        assert filenames == {"top.pdf", "nested.xlsx"}

    def test_inline_content_without_attachment_id_is_skipped(self):
        # Inline images referenced by Content-ID have no attachmentId on
        # their own body — e.g. small images sent as base64 `data` directly.
        payload = {
            "mimeType": "multipart/related",
            "parts": [
                {"filename": "logo.png", "mimeType": "image/png", "body": {"data": "iVBORw0KG..."}},
            ],
        }
        assert _walk_attachment_parts(payload) == []

    def test_top_level_non_multipart_with_attachment_id(self):
        # Rare but possible: a message whose payload IS the attachment part
        # directly, no parts array.
        payload = _attachment_part("solo.pdf", "application/pdf", "ATT_SOLO")
        found = _walk_attachment_parts(payload)
        assert len(found) == 1
        assert found[0]["filename"] == "solo.pdf"

    def test_no_parts_no_filename_returns_empty(self):
        assert _walk_attachment_parts({"mimeType": "text/plain", "body": {"data": "x"}}) == []


# ---------------------------------------------------------------------------
# db tier — scan_gmail / _scan_gmail_account
# ---------------------------------------------------------------------------

def _full_message(msg_id: str, filename: str, mime_type: str, *, internal_date_ms: int = 1_700_000_000_000) -> dict:
    return {
        "google_message_id": msg_id,
        "thread_id": f"thread-{msg_id}",
        "subject": f"Subject for {msg_id}",
        "sender": "sender@example.com",
        "date": "Mon, 1 Jan 2024 00:00:00 +0000",
        "internal_date": internal_date_ms,
        "attachments": [
            {
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": 12345,
                "attachment_id": f"ATT_{msg_id}",
            },
        ],
    }


@pytest.mark.db
class TestScanGmail:
    def _seed_account(self, db_session, user_id: int, account_email: str):
        from app.auth.encryption import encrypt_token
        from app.models.tokens import OAuthToken

        db_session.add(OAuthToken(
            user_id=user_id,
            provider="google",
            account_email=account_email,
            access_token=encrypt_token("fake-access-token"),
            refresh_token=encrypt_token("fake-refresh-token"),
        ))
        db_session.commit()

    def test_no_connected_accounts_is_a_clean_noop(self, db_session):
        from app.auth.context import use_user
        from app.integrations.attachments.scan import scan_gmail

        with use_user(1):
            result = scan_gmail(db_session)

        assert result["supported"] is True
        assert result["accounts_scanned"] == 0
        assert result["new_pending"] == 0

    def test_scan_creates_rows_and_flags_unsupported_mimetypes(self, db_session):
        """Mimetype-based flagging, isolated from the source gate.

        Gmail is not in SUPPORTED_INGEST_SOURCES today (see
        test_gmail_rows_never_reach_pending_across_rescans below for that
        behaviour on its own), so this test patches the set to include
        'gmail' — as if the download path had landed — purely to exercise
        the *other* gate `_discovery_status` applies: mimetype parseability.
        """
        from app.auth.context import use_user
        from app.integrations.attachments.models import MessageAttachment
        from app.integrations.attachments.scan import scan_gmail

        self._seed_account(db_session, user_id=1, account_email="alex@example.com")

        candidates = ["m1", "m2"]
        full = [
            _full_message("m1", "invoice.pdf", "application/pdf"),
            _full_message("m2", "photo.jpg", "image/jpeg"),  # not in PARSEABLE_MIMES
        ]

        with (
            use_user(1),
            patch("app.integrations.attachments.scan.SUPPORTED_INGEST_SOURCES", frozenset({"whatsapp", "gmail"})),
            patch(
                "app.integrations.google_mail.client.list_attachment_candidates_page",
                return_value=(candidates, None),  # no next page -> backfill completes
            ),
            patch(
                "app.integrations.google_mail.client.fetch_messages_full",
                return_value=full,
            ),
        ):
            result = scan_gmail(db_session)

        assert result["accounts_scanned"] == 1
        assert result["new_pending"] == 1  # only the pdf is "parseable"
        assert result["unsupported"] == 1  # the jpg

        rows = (
            db_session.query(MessageAttachment)
            .filter_by(user_id=1, source="gmail")
            .order_by(MessageAttachment.message_ref)
            .all()
        )
        assert [r.message_ref for r in rows] == ["m1", "m2"]
        pdf_row = next(r for r in rows if r.message_ref == "m1")
        assert pdf_row.filename == "invoice.pdf"
        assert pdf_row.mime_type == "application/pdf"
        assert pdf_row.parse_status == "pending"
        assert pdf_row.storage_path == "ATT_m1"  # Gmail attachmentId, not a local path
        jpg_row = next(r for r in rows if r.message_ref == "m2")
        assert jpg_row.parse_status == "unsupported"
        assert jpg_row.skip_reason

    def test_calendar_invite_with_two_mime_parts_makes_one_row(self, db_session):
        """The regression that killed Gmail discovery outright.

        A Google Calendar invite carries `invite.ics` as TWO MIME parts —
        `text/calendar` and `application/ics`. `uq_msg_attachment` is
        (user_id, source, message_ref, filename) with no mime_type, so the
        two parts collided with each other inside one transaction; the flush
        aborted the whole Gmail pass with a UniqueViolation, so no user with
        a calendar invite in range ever completed a scan.
        """
        from app.auth.context import use_user
        from app.integrations.attachments.models import MessageAttachment
        from app.integrations.attachments.scan import scan_gmail

        self._seed_account(db_session, user_id=1, account_email="alex@example.com")

        invite = _full_message("m1", "invite.ics", "text/calendar")
        invite["attachments"].append({
            "filename": "invite.ics",
            "mime_type": "application/ics",
            "size_bytes": 12345,
            "attachment_id": "ATT_m1_b",
        })

        with (
            use_user(1),
            patch(
                "app.integrations.google_mail.client.list_attachment_candidates_page",
                return_value=(["m1"], None),
            ),
            patch(
                "app.integrations.google_mail.client.fetch_messages_full",
                return_value=[invite],
            ),
        ):
            result = scan_gmail(db_session)  # must not raise

        assert result["accounts_scanned"] == 1
        rows = (
            db_session.query(MessageAttachment)
            .filter_by(user_id=1, source="gmail", message_ref="m1")
            .all()
        )
        assert len(rows) == 1
        # First part wins, so the row is stable across rescans.
        assert rows[0].mime_type == "text/calendar"
        assert rows[0].storage_path == "ATT_m1"

    def test_a_real_second_attachment_is_not_swallowed_by_the_dedup(self):
        """The dedup keys on filename, so two genuinely different files on one
        message must both survive — otherwise the fix for the invite case
        silently loses attachments.

        Both land 'unsupported' (not 'pending') because gmail isn't in
        SUPPORTED_INGEST_SOURCES — that's the fix under test elsewhere; the
        point here is purely that dedup doesn't eat one of the two rows."""
        from app.integrations.attachments import scan as scan_mod

        msg = _full_message("m1", "invoice.pdf", "application/pdf")
        msg["attachments"].append({
            "filename": "contract.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 999,
            "attachment_id": "ATT_m1_b",
        })

        added = []
        session = SimpleNamespace(add=added.append)
        created, unsupported = scan_mod._insert_gmail_attachments(
            session, 1, "alex@example.com", [msg],
        )
        assert (created, unsupported) == (0, 2)
        assert sorted(a.filename for a in added) == ["contract.pdf", "invoice.pdf"]

    def test_unnamed_parts_are_not_deduped_against_each_other(self):
        """`filename` is nullable and Postgres does not collide NULLs, so two
        unnamed parts are two legitimate rows. Putting `None` in the seen set
        would have thrown the second one away."""
        from app.integrations.attachments import scan as scan_mod

        msg = _full_message("m1", "invoice.pdf", "application/pdf")
        msg["attachments"] = [
            {"filename": None, "mime_type": "application/pdf", "size_bytes": 1, "attachment_id": "a"},
            {"filename": None, "mime_type": "application/pdf", "size_bytes": 2, "attachment_id": "b"},
        ]

        added = []
        session = SimpleNamespace(add=added.append)
        _, unsupported = scan_mod._insert_gmail_attachments(
            session, 1, "alex@example.com", [msg],
        )
        assert unsupported == 2  # gmail source, not mimetype — but still 2 distinct rows
        assert len(added) == 2

    def test_rescan_is_idempotent_no_duplicate_rows_no_refetch(self, db_session):
        from app.auth.context import use_user
        from app.integrations.attachments.models import MessageAttachment
        from app.integrations.attachments.scan import scan_gmail

        self._seed_account(db_session, user_id=1, account_email="alex@example.com")

        candidates = ["m1"]
        full = [_full_message("m1", "invoice.pdf", "application/pdf")]

        with (
            use_user(1),
            patch(
                "app.integrations.google_mail.client.list_attachment_candidates_page",
                return_value=(candidates, None),
            ) as mock_list,
            patch(
                "app.integrations.google_mail.client.fetch_messages_full",
                return_value=full,
            ) as mock_fetch,
        ):
            first = scan_gmail(db_session)
            second = scan_gmail(db_session)

        assert first["unsupported"] == 1  # gmail isn't a supported ingest source
        assert second["unsupported"] == 0  # already recorded, nothing new

        rows = (
            db_session.query(MessageAttachment)
            .filter_by(user_id=1, source="gmail", message_ref="m1")
            .all()
        )
        assert len(rows) == 1  # no duplicate despite two scan_gmail calls

        # fetch_messages_full should only ever have been called with unscanned
        # ids — by the second scan, "m1" is already recorded so it must not
        # appear in any call's argument list.
        for call in mock_fetch.call_args_list[1:]:
            assert "m1" not in call.args[2]

    def test_backfill_cursor_persists_across_calls(self, db_session):
        from app.auth.context import use_user
        from app.integrations.attachments.scan import _gmail_cursor_key, scan_gmail
        from app.models.tokens import SyncState

        self._seed_account(db_session, user_id=1, account_email="alex@example.com")

        # First call: catch-up page (page1) and backfill page (page_token=None)
        # both hit the same mock in this simplified test, returning a page
        # token so backfill is NOT marked done.
        with (
            use_user(1),
            patch(
                "app.integrations.google_mail.client.list_attachment_candidates_page",
                return_value=(["m1"], "PAGE_TOKEN_2"),
            ),
            patch(
                "app.integrations.google_mail.client.fetch_messages_full",
                return_value=[_full_message("m1", "invoice.pdf", "application/pdf")],
            ),
        ):
            scan_gmail(db_session)

        key = _gmail_cursor_key(1, "alex@example.com")
        state = db_session.query(SyncState).filter_by(integration=key).first()
        assert state is not None
        import json
        cursor = json.loads(state.last_error)
        assert cursor["page_token"] == "PAGE_TOKEN_2"
        assert cursor["backfill_done"] is False

    def test_account_scoping_only_scans_requesting_users_accounts(self, db_session):
        from app.auth.context import use_user
        from app.integrations.attachments.models import MessageAttachment
        from app.integrations.attachments.scan import scan_gmail

        self._seed_account(db_session, user_id=1, account_email="alex@example.com")
        self._seed_account(db_session, user_id=2, account_email="sam@example.com")

        with (
            use_user(1),
            patch(
                "app.integrations.google_mail.client.list_attachment_candidates_page",
                return_value=(["m1"], None),
            ),
            patch(
                "app.integrations.google_mail.client.fetch_messages_full",
                return_value=[_full_message("m1", "invoice.pdf", "application/pdf")],
            ),
        ):
            result = scan_gmail(db_session)

        # Only alex's account should have been scanned, not sam's.
        assert result["accounts_scanned"] == 1
        assert result["accounts"][0]["account"] == "alex@example.com"

        rows = db_session.query(MessageAttachment).filter_by(source="gmail").all()
        assert all(r.user_id == 1 for r in rows)

    def test_needs_reauth_on_one_account_does_not_block_others(self, db_session):
        from app.auth.context import use_user
        from app.errors import NeedsReauthError
        from app.integrations.attachments.scan import scan_gmail

        self._seed_account(db_session, user_id=1, account_email="dead@example.com")
        self._seed_account(db_session, user_id=1, account_email="alive@example.com")

        def _fake_list(account_email, session, *, user_id, page_token=None, max_results=100):
            if account_email == "dead@example.com":
                raise NeedsReauthError(account_email, "revoked")
            return (["m1"], None)

        with (
            use_user(1),
            patch(
                "app.integrations.google_mail.client.list_attachment_candidates_page",
                side_effect=_fake_list,
            ),
            patch(
                "app.integrations.google_mail.client.fetch_messages_full",
                return_value=[_full_message("m1", "invoice.pdf", "application/pdf")],
            ),
        ):
            result = scan_gmail(db_session)

        assert result["accounts_scanned"] == 2
        assert result["unsupported"] == 1  # only the alive account contributed (gmail always unsupported)
        by_account = {a.get("account"): a for a in result["accounts"]}
        assert "error" in by_account["dead@example.com"]
        assert "error" not in by_account["alive@example.com"]

    def test_gmail_rows_never_reach_pending_across_rescans(self, db_session):
        """Acceptance: attachments_pending must return no Gmail rows, and a
        rescan must not re-queue them as pending.

        Direct regression test for the bug this fix targets: `scan` used to
        register every Gmail attachment as 'pending' while `ingest`
        unconditionally rejected `source='gmail'` — a queue that filled and
        never drained. Runs scan_gmail twice (mimicking the periodic cron)
        and asserts no gmail row is ever 'pending' after either call.
        """
        from app.auth.context import use_user
        from app.integrations.attachments.models import MessageAttachment
        from app.integrations.attachments.scan import scan_gmail

        self._seed_account(db_session, user_id=1, account_email="alex@example.com")

        full = [_full_message("m1", "invoice.pdf", "application/pdf")]

        with (
            use_user(1),
            patch(
                "app.integrations.google_mail.client.list_attachment_candidates_page",
                return_value=(["m1"], None),
            ),
            patch(
                "app.integrations.google_mail.client.fetch_messages_full",
                return_value=full,
            ),
        ):
            scan_gmail(db_session)  # first scan
            scan_gmail(db_session)  # rescan — must not re-queue as pending

        pending_gmail = (
            db_session.query(MessageAttachment)
            .filter_by(user_id=1, source="gmail", parse_status="pending")
            .all()
        )
        assert pending_gmail == []

        row = db_session.query(MessageAttachment).filter_by(
            user_id=1, source="gmail", message_ref="m1",
        ).one()
        assert row.parse_status == "unsupported"
        assert row.skip_reason == "source 'gmail' not supported yet"


class TestIngestSourceGate:
    """`ingest_one` and `scan`'s discovery status must agree on which
    sources are supported — both read `attachments/sources.py`."""

    def _seed_row(self, db_session, *, source: str, parse_status: str = "pending") -> int:
        from app.integrations.attachments.models import MessageAttachment

        row = MessageAttachment(
            user_id=1,
            source=source,
            message_ref="m1",
            filename="invoice.pdf",
            mime_type="application/pdf",
            parse_status=parse_status,
        )
        db_session.add(row)
        db_session.commit()
        return row.id

    @pytest.mark.db
    def test_ingest_rejects_gmail_using_the_shared_set(self, db_session):
        """Not just "does ingest reject gmail" (it always did) — this pins
        ingest.py to actually READ `sources.SUPPORTED_INGEST_SOURCES` rather
        than deciding independently. Before the fix, ingest.py's own inline
        `!= "whatsapp"` check happened to produce an identical-looking error
        string by coincidence, so a plain string-equality assertion here
        passed even with the shared set deleted — this identity check is
        what actually fails in that case (module has no such attribute)."""
        from app.integrations.attachments import ingest as ingest_mod
        from app.integrations.attachments import sources as sources_mod

        assert ingest_mod.SUPPORTED_INGEST_SOURCES is sources_mod.SUPPORTED_INGEST_SOURCES

        att_id = self._seed_row(db_session, source="gmail")
        result = ingest_mod.ingest_one(db_session, att_id)

        assert result["status"] == "error"
        assert result["detail"] == sources_mod.unsupported_source_reason("gmail")

    @pytest.mark.db
    def test_one_line_change_reenables_gmail_on_both_sides(self, db_session, monkeypatch):
        """The acceptance test for the whole fix: adding 'gmail' to the one
        shared set is enough to (a) make scan queue it pending again and
        (b) stop ingest rejecting it for its source — with no other edit."""
        from app.integrations.attachments import ingest as ingest_mod
        from app.integrations.attachments import scan as scan_mod
        from app.integrations.attachments.models import MessageAttachment

        patched = frozenset({"whatsapp", "gmail"})
        monkeypatch.setattr(scan_mod, "SUPPORTED_INGEST_SOURCES", patched)
        monkeypatch.setattr(ingest_mod, "SUPPORTED_INGEST_SOURCES", patched)

        # (a) scan side: a fresh gmail pdf attachment is now 'pending'.
        status, reason = scan_mod._discovery_status("gmail", "application/pdf")
        assert (status, reason) == ("pending", None)

        # (b) ingest side: the source gate no longer fires. Force a download
        # failure immediately after the gate so the test doesn't need a real
        # WhatsApp bridge — the point is which check rejects it, not what
        # happens after.
        monkeypatch.setattr(
            ingest_mod, "_download",
            lambda message_ref, dest: (_ for _ in ()).throw(RuntimeError("no bridge in test")),
        )
        att_id = self._seed_row(db_session, source="gmail")
        result = ingest_mod.ingest_one(db_session, att_id)

        assert result["status"] == "failed"  # NOT "error" / source-unsupported
        assert "download failed" in result["detail"]
        assert "not supported" not in result["detail"]

        row = db_session.get(MessageAttachment, att_id)
        assert row.parse_status == "failed"
