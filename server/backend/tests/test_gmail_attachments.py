"""Gmail attachment scanning tests.

Unit tier: `_walk_attachment_parts` (pure MIME-tree walker, no I/O) against a
nested multipart fixture.

db tier: `scan_gmail` / `_scan_gmail_account` against real Postgres, with the
Gmail API boundary (`list_attachment_candidates_page`, `fetch_messages_full`)
monkeypatched — exercises candidate selection, idempotency (re-scan doesn't
duplicate rows or re-fetch already-recorded messages), the checkpoint cursor,
and per-user account scoping.
"""

import json
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
        """Mimetype-based flagging. Gmail is in SUPPORTED_INGEST_SOURCES
        (since the download path landed, 2026-09-07), so the only gate left
        for `_discovery_status` to apply here is mimetype parseability.
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
        assert jpg_row.skip_reason == "mimetype 'image/jpeg' not parseable"

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

        Both land 'pending' (gmail is a supported source and pdf has a
        parser); the point here is purely that dedup doesn't eat one of the
        two rows."""
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
        assert (created, unsupported) == (2, 0)
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
        created, _ = scan_mod._insert_gmail_attachments(
            session, 1, "alex@example.com", [msg],
        )
        assert created == 2  # 2 distinct rows, both pending
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

        assert first["new_pending"] == 1
        assert second["new_pending"] == 0  # already recorded, nothing new
        assert second["unsupported"] == 0

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
        assert result["new_pending"] == 1  # only the alive account contributed
        by_account = {a.get("account"): a for a in result["accounts"]}
        assert "error" in by_account["dead@example.com"]
        assert "error" not in by_account["alive@example.com"]

    def test_gmail_pdf_is_pending_and_stays_one_row_across_rescans(self, db_session):
        """Acceptance for the Gmail download path: a parseable Gmail
        attachment is queued 'pending' (attachments_pending lists it under
        the default filter), and a rescan neither duplicates nor re-flips it.

        This test used to assert the opposite — that no Gmail row ever
        reached 'pending' — because ingest could not consume one. The
        scan/ingest agreement that assertion really guarded is pinned by
        TestIngestSourceGate below.
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
            scan_gmail(db_session)  # rescan — must not duplicate

        rows = (
            db_session.query(MessageAttachment)
            .filter_by(user_id=1, source="gmail", message_ref="m1")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].parse_status == "pending"
        assert rows[0].skip_reason is None
        assert rows[0].storage_path == "ATT_m1"


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
    def test_ingest_rejects_an_unknown_source_using_the_shared_set(self, db_session):
        """Pins ingest.py to actually READ `sources.SUPPORTED_INGEST_SOURCES`
        rather than deciding independently — the identity check is what
        fails if ingest grows its own inline set again. Uses a source that
        is in neither set (gmail joined the supported set 2026-09-07, so it
        no longer serves as the rejected example)."""
        from app.integrations.attachments import ingest as ingest_mod
        from app.integrations.attachments import sources as sources_mod

        assert ingest_mod.SUPPORTED_INGEST_SOURCES is sources_mod.SUPPORTED_INGEST_SOURCES
        assert "carrier_pigeon" not in sources_mod.SUPPORTED_INGEST_SOURCES

        att_id = self._seed_row(db_session, source="carrier_pigeon")
        result = ingest_mod.ingest_one(db_session, att_id)

        assert result["status"] == "error"
        assert result["detail"] == sources_mod.unsupported_source_reason("carrier_pigeon")

    def test_discovery_status_for_gmail_is_gated_on_mimetype_only(self):
        """Scan side of the agreement, without a DB: gmail is in the shared
        set, so `_discovery_status` queues a parseable mimetype 'pending'
        and an unparseable one 'unsupported' with the MIMETYPE reason —
        never the source reason."""
        from app.integrations.attachments import scan as scan_mod
        from app.integrations.attachments.sources import unsupported_source_reason

        assert scan_mod._discovery_status("gmail", "application/pdf") == ("pending", None)

        status, reason = scan_mod._discovery_status("gmail", "image/heic")
        assert status == "unsupported"
        assert reason == "mimetype 'image/heic' not parseable"
        assert reason != unsupported_source_reason("gmail")

    def test_discovery_status_mutation_check_removing_gmail_reinstates_the_source_reason(self, monkeypatch):
        """Reinstate the pre-fix shape (gmail absent from the shared set) and
        confirm the test above would fail: the source gate fires first and
        the mimetype is never consulted."""
        from app.integrations.attachments import scan as scan_mod
        from app.integrations.attachments.sources import unsupported_source_reason

        monkeypatch.setattr(scan_mod, "SUPPORTED_INGEST_SOURCES", frozenset({"whatsapp"}))
        assert scan_mod._discovery_status("gmail", "application/pdf") == (
            "unsupported", unsupported_source_reason("gmail"),
        )

    @pytest.mark.db
    def test_gmail_passes_the_source_gate_and_reaches_the_gmail_fetcher(self, db_session, monkeypatch):
        """Ingest side of the agreement: a gmail row is no longer rejected
        for its source; it reaches the per-source fetcher. The WhatsApp
        bridge download is patched to blow up distinctly so the test can
        tell "went to the Gmail path" from "went to the bridge"."""
        from app.integrations.attachments import ingest as ingest_mod
        from app.integrations.attachments.models import MessageAttachment

        monkeypatch.setattr(
            ingest_mod, "_download",
            lambda message_ref, dest: (_ for _ in ()).throw(RuntimeError("WRONG PATH: whatsapp bridge")),
        )
        monkeypatch.setattr(
            ingest_mod, "_download_gmail",
            lambda session, att, dest: (_ for _ in ()).throw(RuntimeError("gmail fetcher reached")),
        )
        att_id = self._seed_row(db_session, source="gmail")
        result = ingest_mod.ingest_one(db_session, att_id)

        assert result["status"] == "failed"  # NOT "error" / source-unsupported
        assert result["detail"] == "download failed: gmail fetcher reached"

        row = db_session.get(MessageAttachment, att_id)
        assert row.parse_status == "failed"


# ---------------------------------------------------------------------------
# attachments_pending's status-counts breakdown
# ---------------------------------------------------------------------------

@pytest.mark.db
class TestPendingStatusCounts:
    """`attachments_pending` filters `results` to one status (default
    'pending'), which is exactly what hid the Gmail bug in the first place —
    a caller who only ever looks at the 'pending' page cannot tell that 60
    Gmail rows are silently stuck under a different status. `counts` reports
    every status's total (plus top skip_reason values) regardless of the
    page filter, so the queue's overall shape is always visible.
    """

    def _row(self, *, source: str, status: str, reason: str | None = None, ref: str) -> "MessageAttachment":
        from app.integrations.attachments.models import MessageAttachment

        return MessageAttachment(
            user_id=1, source=source, message_ref=ref, filename=f"{ref}.pdf",
            mime_type="application/pdf", parse_status=status, skip_reason=reason,
        )

    def test_counts_cover_every_status_not_just_the_filtered_page(self, db_session):
        from app.auth.context import use_user
        from app.integrations.attachments.tools import mcp_tools

        db_session.add_all([
            self._row(source="whatsapp", status="pending", ref="p1"),
            self._row(source="gmail", status="unsupported", ref="g1",
                      reason="source 'gmail' not supported yet"),
            self._row(source="gmail", status="unsupported", ref="g2",
                      reason="source 'gmail' not supported yet"),
            self._row(source="whatsapp", status="unsupported", ref="w1",
                      reason="mimetype 'image/jpeg' not parseable"),
            self._row(source="whatsapp", status="failed", ref="w2",
                      reason="download failed: timeout"),
        ])
        db_session.commit()

        tool = next(t for t in mcp_tools() if t["name"] == "attachments_pending")
        with use_user(1):
            out = json.loads(tool["handler"](db_session, {"status": "pending"}))

        # The filtered page only ever shows 'pending' — one row.
        assert out["count"] == 1
        # But counts.by_status sees the whole queue, including the 4 rows
        # that never appear on this page.
        assert out["counts"]["by_status"] == {
            "pending": 1, "ingested": 0, "skipped": 0, "failed": 1, "unsupported": 3,
        }

    def test_unsupported_top_reasons_are_grouped_and_counted(self, db_session):
        from app.auth.context import use_user
        from app.integrations.attachments.tools import mcp_tools

        db_session.add_all([
            self._row(source="gmail", status="unsupported", ref="g1",
                      reason="source 'gmail' not supported yet"),
            self._row(source="gmail", status="unsupported", ref="g2",
                      reason="source 'gmail' not supported yet"),
            self._row(source="whatsapp", status="unsupported", ref="w1",
                      reason="mimetype 'image/jpeg' not parseable"),
        ])
        db_session.commit()

        tool = next(t for t in mcp_tools() if t["name"] == "attachments_pending")
        with use_user(1):
            out = json.loads(tool["handler"](db_session, {"status": "unsupported"}))

        reasons = {r["reason"]: r["count"] for r in out["counts"]["top_reasons"]["unsupported"]}
        assert reasons == {
            "source 'gmail' not supported yet": 2,
            "mimetype 'image/jpeg' not parseable": 1,
        }
        # 'pending' never gets a reasons breakdown — nothing has failed to
        # queue yet, so there's nothing to explain.
        assert "pending" not in out["counts"]["top_reasons"]

    def test_counts_respect_the_source_filter(self, db_session):
        from app.auth.context import use_user
        from app.integrations.attachments.tools import mcp_tools

        db_session.add_all([
            self._row(source="gmail", status="unsupported", ref="g1",
                      reason="source 'gmail' not supported yet"),
            self._row(source="whatsapp", status="pending", ref="w1"),
        ])
        db_session.commit()

        tool = next(t for t in mcp_tools() if t["name"] == "attachments_pending")
        with use_user(1):
            gmail_only = json.loads(tool["handler"](db_session, {"status": "unsupported", "source": "gmail"}))
            wa_only = json.loads(tool["handler"](db_session, {"status": "pending", "source": "whatsapp"}))

        assert gmail_only["counts"]["by_status"]["unsupported"] == 1
        assert gmail_only["counts"]["by_status"]["pending"] == 0
        assert wa_only["counts"]["by_status"]["pending"] == 1
        assert wa_only["counts"]["by_status"]["unsupported"] == 0

    def test_mutation_check_status_filter_alone_would_hide_the_gmail_pileup(self, db_session, monkeypatch):
        """Reinstate the exact shape of the original bug — scan queuing Gmail
        as 'pending' forever — against the counts feature itself: if
        `_status_counts` were deleted and attachments_pending fell back to
        reporting only the filtered page's own length as if it were the
        total, a caller filtering on 'pending' would see a healthy '1' while
        60 gmail rows sat unsupported. This test fails on that reverted
        shape and passes on the real implementation, proving the counts
        assertions above are actually exercising the fix rather than
        trivially passing regardless."""
        from app.auth.context import use_user
        from app.integrations.attachments import tools as tools_mod

        db_session.add_all([
            self._row(source="whatsapp", status="pending", ref="p1"),
            self._row(source="gmail", status="unsupported", ref="g1",
                      reason="source 'gmail' not supported yet"),
        ])
        db_session.commit()

        # Simulate the pre-fix handler: no breakdown, count-of-page only.
        def _broken_pending_handler(session, arguments):
            status = arguments.get("status") or "pending"
            rows = (
                tools_mod.scoped_query(session, tools_mod.MessageAttachment)
                .filter(tools_mod.MessageAttachment.parse_status == status)
                .all()
            )
            return json.dumps({"count": len(rows), "status_filter": status})

        with use_user(1):
            broken = json.loads(_broken_pending_handler(db_session, {"status": "pending"}))
            assert "counts" not in broken  # the regression this test guards against

            real = json.loads(tools_mod.attachments_pending_handler(db_session, {"status": "pending"}))
            assert real["counts"]["by_status"]["unsupported"] == 1  # visible even though filtered page is 'pending'


# ---------------------------------------------------------------------------
# alembic/versions/2026_08_24_..._gmail_attachments_unsupported.py — data
# backfill idempotency. The migration itself can't be imported directly (it
# runs through `op.execute`, which needs a live alembic MigrationContext),
# so this exercises the identical SQL shape it applies, against a real
# session, to prove a second run is a no-op.
# ---------------------------------------------------------------------------

class TestUnsupportedBackfillIdempotent:
    _REASON = "source 'gmail' not supported yet"

    def _apply_backfill(self, session) -> int:
        from sqlalchemy import text as sa_text

        result = session.execute(
            sa_text(
                "UPDATE message_attachments "
                "SET parse_status = 'unsupported', skip_reason = :reason "
                "WHERE source = 'gmail' AND parse_status = 'pending'"
            ),
            {"reason": self._REASON},
        )
        session.commit()
        return result.rowcount

    @pytest.mark.db
    def test_second_run_is_a_noop(self, db_session):
        from app.integrations.attachments.models import MessageAttachment

        db_session.add_all([
            MessageAttachment(user_id=1, source="gmail", message_ref="g1", filename="a.pdf",
                               mime_type="application/pdf", parse_status="pending"),
            MessageAttachment(user_id=1, source="gmail", message_ref="g2", filename="b.pdf",
                               mime_type="application/pdf", parse_status="pending"),
            # Already ingested (e.g. hand-fixed) — must survive untouched.
            MessageAttachment(user_id=1, source="gmail", message_ref="g3", filename="c.pdf",
                               mime_type="application/pdf", parse_status="ingested"),
            # Non-gmail pending row must never be touched by this backfill.
            MessageAttachment(user_id=1, source="whatsapp", message_ref="w1", filename="d.pdf",
                               mime_type="application/pdf", parse_status="pending"),
        ])
        db_session.commit()

        first_pass = self._apply_backfill(db_session)
        second_pass = self._apply_backfill(db_session)

        assert first_pass == 2  # g1, g2
        assert second_pass == 0  # nothing left matching WHERE parse_status = 'pending'

        rows = {
            r.message_ref: (r.parse_status, r.skip_reason)
            for r in db_session.query(MessageAttachment).all()
        }
        assert rows["g1"] == ("unsupported", self._REASON)
        assert rows["g2"] == ("unsupported", self._REASON)
        assert rows["g3"] == ("ingested", None)  # untouched
        assert rows["w1"] == ("pending", None)  # untouched — not gmail


# ---------------------------------------------------------------------------
# google_mail/client.py::fetch_attachment — the Gmail byte fetcher
# ---------------------------------------------------------------------------

def _http_error(status: int):
    import httplib2
    from googleapiclient.errors import HttpError

    resp = httplib2.Response({"status": str(status)})
    return HttpError(resp, b"error body")


class _FakeGmailService:
    """Mimics `service.users().messages().attachments().get(...).execute()`,
    recording the kwargs `get` was called with."""

    def __init__(self, *, result: dict | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.get_calls: list[dict] = []

    # chain: users() -> messages() -> attachments() -> get(**kw) -> execute()
    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return self

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class TestFetchAttachment:
    def test_decodes_base64url_payload_to_bytes(self):
        """Gmail returns base64url (`-`/`_` alphabet) without padding; the
        fetcher must hand back the raw bytes. The payload here is chosen so
        standard base64 and base64url differ (it contains `-` and `_`) and so
        that padding has to be restored — bytes.fromhex is a plain oracle."""
        import base64

        from app.integrations.google_mail import client

        raw = bytes(range(256)) + b"%PDF-1.7 tail"  # every byte value -> both '-' and '_' appear
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        assert "-" in encoded and "_" in encoded and not encoded.endswith("=")

        service = _FakeGmailService(result={"size": len(raw), "data": encoded})
        with patch.object(client, "get_gmail_service", return_value=service) as gs:
            out = client.fetch_attachment(
                "alex@example.com", object(), "MSG_1", "ATT_1", user_id=7,
            )

        assert out == raw
        # The token lookup is scoped to the user we were given, nothing else.
        assert gs.call_args.kwargs == {"user_id": 7}
        assert service.get_calls == [{"userId": "me", "messageId": "MSG_1", "id": "ATT_1"}]

    def test_mutation_check_standard_base64_would_corrupt_the_bytes(self):
        """Prove the base64url decode is load-bearing: decoding the same
        payload as plain base64 (the mutation) does not reproduce the file."""
        import base64
        import binascii

        raw = bytes(range(256))
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        padded = encoded + "=" * (-len(encoded) % 4)
        try:
            wrong = base64.b64decode(padded)
        except binascii.Error:
            wrong = None
        assert wrong != raw

    def test_404_raises_the_dedicated_gone_error(self):
        from app.errors import PermanentError
        from app.integrations.google_mail import client

        service = _FakeGmailService(error=_http_error(404))
        with patch.object(client, "get_gmail_service", return_value=service):
            with pytest.raises(client.GmailAttachmentGone) as ei:
                client.fetch_attachment("alex@example.com", object(), "MSG_1", "ATT_1", user_id=1)

        assert isinstance(ei.value, PermanentError)  # scheduler contract: don't retry
        assert "404" in str(ei.value)
        assert "MSG_1" in str(ei.value)

    def test_other_http_errors_are_classified_not_gone(self):
        from app.errors import TransientError
        from app.integrations.google_mail import client

        service = _FakeGmailService(error=_http_error(503))
        with patch.object(client, "get_gmail_service", return_value=service):
            with pytest.raises(TransientError):
                client.fetch_attachment("alex@example.com", object(), "MSG_1", "ATT_1", user_id=1)

    def test_no_credentials_returns_none_without_calling_gmail(self):
        from app.integrations.google_mail import client

        with patch.object(client, "get_gmail_service", return_value=None):
            assert client.fetch_attachment("x@example.com", object(), "M", "A", user_id=1) is None


# ---------------------------------------------------------------------------
# ingest.py::_download_gmail — row-level behaviour on top of the fetcher
# ---------------------------------------------------------------------------

class _FakeMailFacade:
    """Stand-in for the `mail.query` capability as `_download_gmail` uses it.

    `fetch_attachment` is driven by a dict of (account -> bytes | Exception);
    every call is recorded with the user_id it was made for, which is the
    assertion that matters: the token used is the row OWNER's.
    """

    class AttachmentGone(Exception):
        pass

    def __init__(self, *, by_account: dict, known_account: str | None = None):
        self.by_account = by_account
        self.known_account = known_account
        self.calls: list[tuple[str, str, str, int]] = []

    def account_for_message(self, session, google_message_id, *, user_id):
        return self.known_account

    def fetch_attachment(self, account_email, session, message_id, attachment_id, *, user_id):
        self.calls.append((account_email, message_id, attachment_id, user_id))
        outcome = self.by_account[account_email]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _seed_google_account(session, user_id: int, account_email: str) -> None:
    from app.auth.encryption import encrypt_token
    from app.models.tokens import OAuthToken

    session.add(OAuthToken(
        user_id=user_id, provider="google", account_email=account_email,
        access_token=encrypt_token("fake-access-token"),
        refresh_token=encrypt_token("fake-refresh-token"),
    ))
    session.commit()


def _seed_gmail_row(session, *, user_id: int, message_ref: str = "MSG_1", attachment_id: str = "ATT_1"):
    from app.integrations.attachments.models import MessageAttachment

    row = MessageAttachment(
        user_id=user_id, source="gmail", message_ref=message_ref,
        filename="invoice.pdf", mime_type="application/pdf", size_bytes=1234,
        parse_status="pending", storage_path=attachment_id,
    )
    session.add(row)
    session.commit()
    return row


@pytest.mark.db
class TestGmailIngestUsesOwnerToken:
    """`ingest_one` on a gmail row: the Gmail call is made with the row
    owner's user_id and the attachmentId stored on the row; a 404 lands as
    `failed` with a reason that names the cause; a foreign id is refused
    before any Gmail call happens (extends the PR #121 scoping tests, which
    only had WhatsApp rows to work with)."""

    @pytest.fixture
    def facade(self, monkeypatch):
        holder: dict = {}

        def _install(fake):
            from app.integrations.attachments import ingest as ingest_mod

            monkeypatch.setattr(
                ingest_mod, "get_capability",
                lambda name: fake if name == "mail.query" else ingest_mod._corpus,
            )
            holder["fake"] = fake
            return fake

        return _install

    def test_fetch_is_made_with_the_row_owners_token_and_stored_attachment_id(
        self, db_session, facade, monkeypatch, tmp_path,
    ):
        from app.auth.context import use_user
        from app.integrations.attachments import ingest as ingest_mod

        monkeypatch.setattr(ingest_mod, "STAGING_DIR", tmp_path)
        fake = facade(_FakeMailFacade(
            by_account={"sam@example.com": b"%PDF-1.7 not really"},
            known_account="sam@example.com",
        ))
        row = _seed_gmail_row(db_session, user_id=2)

        with use_user(2):
            result = ingest_mod.ingest_one(db_session, row.id)

        assert fake.calls == [("sam@example.com", "MSG_1", "ATT_1", 2)]
        assert (tmp_path / f"{row.id}_invoice.pdf").read_bytes() == b"%PDF-1.7 not really"
        # The pdf parser tolerates the stub bytes, so this runs end to end:
        # the document lands in the corpus typed as a Gmail attachment.
        assert result["status"] == "ingested"
        from app.integrations.historical_corpus.models import HistoricalDocument

        doc = db_session.get(HistoricalDocument, result["historical_doc_id"])
        assert doc.source_type == "gmail_attachment_pdf"
        assert doc.source_path == f"gmail_attachment/{row.id}/invoice.pdf"
        assert doc.doc_metadata["attachment_source"] == "gmail"
        assert doc.doc_metadata["gmail_message_ref"] == "MSG_1"
        assert "wa_message_ref" not in doc.doc_metadata
        db_session.expire_all()
        refreshed = db_session.get(type(row), row.id)
        assert refreshed.parse_status == "ingested"
        assert refreshed.historical_doc_id == doc.id

    def test_mutation_check_caller_id_is_not_what_reaches_gmail(self, db_session, facade, monkeypatch, tmp_path):
        """Explicit `user_id=` (the scheduler shape) on someone else's row:
        the Gmail call still carries the ROW owner. If `_download_gmail`
        used `current_user_id()` instead of `att.user_id`, this would record
        user 1."""
        from app.auth.context import use_user
        from app.integrations.attachments import ingest as ingest_mod

        monkeypatch.setattr(ingest_mod, "STAGING_DIR", tmp_path)
        fake = facade(_FakeMailFacade(
            by_account={"sam@example.com": b"bytes"}, known_account="sam@example.com",
        ))
        row = _seed_gmail_row(db_session, user_id=2)

        with use_user(1):
            ingest_mod.ingest_one(db_session, row.id, user_id=2)

        assert [c[3] for c in fake.calls] == [2]

    def test_foreign_gmail_row_is_refused_before_any_gmail_call(self, db_session, facade):
        from app.auth.context import use_user
        from app.integrations.attachments import ingest as ingest_mod
        from app.integrations.attachments.models import MessageAttachment

        fake = facade(_FakeMailFacade(
            by_account={"sam@example.com": b"bytes"}, known_account="sam@example.com",
        ))
        theirs = _seed_gmail_row(db_session, user_id=2)

        with use_user(1):
            result = ingest_mod.ingest_one(db_session, theirs.id)

        assert result == {"id": theirs.id, "status": "error", "detail": "not found"}
        assert fake.calls == []
        db_session.expire_all()
        assert db_session.get(MessageAttachment, theirs.id).parse_status == "pending"

    def test_404_marks_the_row_failed_with_a_reason_naming_the_rotated_id(self, db_session, facade, monkeypatch, tmp_path):
        from app.auth.context import use_user
        from app.integrations.attachments import ingest as ingest_mod
        from app.integrations.attachments.models import MessageAttachment

        monkeypatch.setattr(ingest_mod, "STAGING_DIR", tmp_path)
        fake = facade(_FakeMailFacade(
            by_account={"alex@example.com": _FakeMailFacade.AttachmentGone("404")},
            known_account="alex@example.com",
        ))
        row = _seed_gmail_row(db_session, user_id=1)

        with use_user(1):
            result = ingest_mod.ingest_one(db_session, row.id)

        assert result["status"] == "failed"
        db_session.expire_all()
        row = db_session.get(MessageAttachment, row.id)
        assert row.parse_status == "failed"
        assert row.skip_reason.startswith("download failed: ")
        assert "404" in row.skip_reason
        assert "MSG_1" in row.skip_reason
        assert "not stable" in row.skip_reason
        assert fake.calls == [("alex@example.com", "MSG_1", "ATT_1", 1)]

    def test_unknown_mailbox_tries_each_of_the_owners_accounts_until_one_answers(
        self, db_session, facade, monkeypatch, tmp_path,
    ):
        """No `mail_messages` cache row for the id: every connected Google
        account of the OWNER is a candidate; a 404 from the wrong mailbox
        moves on to the next rather than failing the row.

        Caller and owner deliberately differ (bound user 1, explicit
        `user_id=2` — the scheduler shape): the candidate list must come
        from the ROW owner's tokens. User 1's own account is connected and
        must never be tried."""
        from app.auth.context import use_user
        from app.integrations.attachments import ingest as ingest_mod

        monkeypatch.setattr(ingest_mod, "STAGING_DIR", tmp_path)
        _seed_google_account(db_session, 2, "a@example.com")
        _seed_google_account(db_session, 2, "b@example.com")
        _seed_google_account(db_session, 1, "alex@example.com")  # the caller — never tried
        fake = facade(_FakeMailFacade(
            by_account={
                "a@example.com": _FakeMailFacade.AttachmentGone("404"),
                "b@example.com": b"bytes",
                "alex@example.com": b"WRONG MAILBOX",
            },
            known_account=None,
        ))
        row = _seed_gmail_row(db_session, user_id=2)

        with use_user(1):
            ingest_mod.ingest_one(db_session, row.id, user_id=2)

        assert [(c[0], c[3]) for c in fake.calls] == [("a@example.com", 2), ("b@example.com", 2)]

    def test_oversize_download_is_reported_against_the_cap(self, db_session, facade, monkeypatch, tmp_path):
        from app.auth.context import use_user
        from app.integrations.attachments import ingest as ingest_mod

        monkeypatch.setattr(ingest_mod, "STAGING_DIR", tmp_path)
        monkeypatch.setattr(ingest_mod, "MAX_BYTES", 10)
        facade(_FakeMailFacade(
            by_account={"alex@example.com": b"x" * 11}, known_account="alex@example.com",
        ))
        # No declared size, so the pre-download cap (`skipped`, same as
        # WhatsApp) cannot fire — the post-download check is what's under test.
        row = _seed_gmail_row(db_session, user_id=1)
        row.size_bytes = None
        db_session.commit()

        with use_user(1):
            result = ingest_mod.ingest_one(db_session, row.id)

        assert result["status"] == "failed"
        assert "over size cap (11 > 10)" in result["detail"]


# ---------------------------------------------------------------------------
# scan.py::reevaluate_unsupported_gmail_rows — the one-off for existing rows
# ---------------------------------------------------------------------------

@pytest.mark.db
class TestReevaluateUnsupportedGmailRows:
    SOURCE_REASON = "source 'gmail' not supported yet"

    def _row(self, *, source="gmail", status="unsupported", reason=None, ref, mime="application/pdf", user_id=1):
        from app.integrations.attachments.models import MessageAttachment

        return MessageAttachment(
            user_id=user_id, source=source, message_ref=ref, filename=f"{ref}.bin",
            mime_type=mime, parse_status=status, skip_reason=reason, storage_path=f"ATT_{ref}",
        )

    def _seed(self, db_session):
        db_session.add_all([
            # The 1,189-row shape: source-unsupported, parseable -> pending
            self._row(ref="pdf", reason=self.SOURCE_REASON),
            self._row(ref="docx", reason=self.SOURCE_REASON,
                      mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            self._row(ref="sam_pdf", reason=self.SOURCE_REASON, user_id=2),
            # source-unsupported, NOT parseable -> stays unsupported, mimetype reason
            self._row(ref="jpg", reason=self.SOURCE_REASON, mime="image/jpeg"),
            self._row(ref="nomime", reason=self.SOURCE_REASON, mime=None),
            # Must be left alone: other reason / other status / other source
            self._row(ref="already_mime", reason="mimetype 'image/heic' not parseable", mime="image/heic"),
            self._row(ref="ingested", status="ingested"),
            self._row(ref="failed", status="failed", reason="download failed: boom"),
            self._row(ref="pending", status="pending"),
            self._row(ref="wa", source="whatsapp", reason=self.SOURCE_REASON.replace("gmail", "whatsapp")),
        ])
        db_session.commit()

    def _snapshot(self, db_session) -> dict:
        from app.integrations.attachments.models import MessageAttachment

        db_session.expire_all()
        return {
            r.message_ref: (r.parse_status, r.skip_reason)
            for r in db_session.query(MessageAttachment).all()
        }

    def test_flips_exactly_the_source_unsupported_rows(self, db_session):
        from app.integrations.attachments.scan import reevaluate_unsupported_gmail_rows

        self._seed(db_session)
        before = self._snapshot(db_session)

        stats = reevaluate_unsupported_gmail_rows(db_session)
        after = self._snapshot(db_session)

        assert stats == {
            "source": "gmail", "selected": 5, "now_pending": 3, "still_unsupported": 2, "dry_run": False,
        }
        assert after["pdf"] == ("pending", None)
        assert after["docx"] == ("pending", None)
        assert after["sam_pdf"] == ("pending", None)  # every owner, each row keeps its user
        assert after["jpg"] == ("unsupported", "mimetype 'image/jpeg' not parseable")
        assert after["nomime"] == ("unsupported", "mimetype None not parseable")
        for untouched in ("already_mime", "ingested", "failed", "pending", "wa"):
            assert after[untouched] == before[untouched], untouched

    def test_second_run_selects_nothing_and_changes_nothing(self, db_session):
        from app.integrations.attachments.scan import reevaluate_unsupported_gmail_rows

        self._seed(db_session)
        reevaluate_unsupported_gmail_rows(db_session)
        once = self._snapshot(db_session)

        stats = reevaluate_unsupported_gmail_rows(db_session)

        assert stats["selected"] == 0
        assert stats["now_pending"] == stats["still_unsupported"] == 0
        assert self._snapshot(db_session) == once

    def test_dry_run_reports_but_writes_nothing(self, db_session):
        from app.integrations.attachments.scan import reevaluate_unsupported_gmail_rows

        self._seed(db_session)
        before = self._snapshot(db_session)

        stats = reevaluate_unsupported_gmail_rows(db_session, dry_run=True)

        assert stats["selected"] == 5 and stats["now_pending"] == 3 and stats["dry_run"] is True
        assert self._snapshot(db_session) == before

    def test_mutation_check_without_the_reason_filter_it_would_touch_mimetype_rows(self, db_session):
        """The selection is `skip_reason == source reason`, not `status ==
        unsupported`. Reinstate the broader selection and show it would have
        rewritten a row this function must leave alone — that is what the
        `already_mime` assertion above is guarding."""
        from app.integrations.attachments.models import MessageAttachment
        from app.integrations.attachments.scan import _discovery_status

        self._seed(db_session)
        broad = (
            db_session.query(MessageAttachment)
            .filter(MessageAttachment.source == "gmail", MessageAttachment.parse_status == "unsupported")
            .all()
        )
        assert {r.message_ref for r in broad} >= {"already_mime"}
        # ...and the mutated version would compute a verdict for it too:
        heic = next(r for r in broad if r.message_ref == "already_mime")
        assert _discovery_status("gmail", heic.mime_type)[0] == "unsupported"

    def test_mutation_check_if_gmail_left_the_set_nothing_is_rewritten(self, db_session, monkeypatch):
        """Guard against the function faking progress: with gmail absent
        from the shared set every verdict is the source reason again, and
        the function must skip rather than rewrite the same value."""
        from app.integrations.attachments import scan as scan_mod

        self._seed(db_session)
        before = self._snapshot(db_session)
        monkeypatch.setattr(scan_mod, "SUPPORTED_INGEST_SOURCES", frozenset({"whatsapp"}))

        stats = scan_mod.reevaluate_unsupported_gmail_rows(db_session)

        assert stats["selected"] == 5
        assert stats["now_pending"] == stats["still_unsupported"] == 0
        assert self._snapshot(db_session) == before
