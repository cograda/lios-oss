"""Issue #140: a `.doc` file that is really OOXML must route to the docx
parser regardless of its extension — content sniffing is the primary signal
everywhere a document's type is inferred (the same lesson `core/CLAUDE.md`'s
Known Issues records for `sniff_kind`'s ftyp/HTML fixes and the twin
`mime_for` bugs: nothing arriving at these ingestion paths has a trustworthy
filename or a trustworthy sender-declared mime_type either).

Two seams, both pure/unit — no DB required:

  1. `historical_corpus.ingest._dispatch_by_suffix` — the corpus's own
     single-file and whole-corpus ingest dispatch.
  2. `attachments.ingest._resolve_parser` — the WhatsApp/Gmail attachment
     path, which starts from a sender-supplied mime_type rather than a
     filename extension, but has exactly the same trust problem.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from app.integrations.historical_corpus.ingest import _dispatch_by_suffix


def _write_real_docx(path, text: str = "hello from a real docx") -> None:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


def _write_fake_zip_docx(path, *, member: str = "word/document.xml") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(member, "<w:document/>")


def _write_ole2_stub(path) -> None:
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504)


class TestCorpusDispatchByContent:
    def test_mislabelled_doc_that_is_really_docx_routes_to_docx_parser(self, tmp_path):
        """The reported shape: a real python-docx document saved with a
        `.doc` extension must still parse successfully end to end (not just
        sniff correctly) — the whole point is that the parser it's handed
        actually works on these bytes."""
        path = tmp_path / "renovation_contract.doc"
        _write_real_docx(path, "the fitted kitchen quote")

        producer = _dispatch_by_suffix(path)
        assert producer is not None

        meta, chunks = producer()
        assert meta.source_type == "docx"
        assert any("fitted kitchen quote" in c.chunk_text for c in chunks)

    def test_correctly_named_docx_is_unaffected(self, tmp_path):
        path = tmp_path / "renovation_contract.docx"
        _write_real_docx(path, "the fitted kitchen quote")

        producer = _dispatch_by_suffix(path)
        assert producer is not None
        meta, chunks = producer()
        assert meta.source_type == "docx"

    def test_zip_masquerading_as_doc_with_no_office_marker_is_unsupported(self, tmp_path):
        """A zip that just isn't an Office document must not be forced
        through the docx parser (it would raise)."""
        path = tmp_path / "photos.doc"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("readme.txt", "hello")

        assert _dispatch_by_suffix(path) is None

    def test_genuine_legacy_doc_ole2_is_still_unsupported(self, tmp_path):
        """A real legacy .doc (OLE2/MS-CFB) is recognised as such and stays
        unsupported (no legacy parser exists) — this must not regress into
        being fed to python-docx, which raises on non-zip input."""
        path = tmp_path / "old_letter.doc"
        _write_ole2_stub(path)

        assert _dispatch_by_suffix(path) is None

    def test_mislabelled_xls_that_is_really_xlsx_bill_of_quantities_routes_to_boq(self, tmp_path):
        path = tmp_path / "Bill of Quantities.xls"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("xl/workbook.xml", "<workbook/>")

        producer = _dispatch_by_suffix(path)
        # openpyxl will raise on this minimal stub, but the point here is
        # dispatch: it must have selected the xlsx/BoQ producer at all,
        # which a suffix-only dispatcher (`.xls` has no case) would not.
        assert producer is not None


# ---------------------------------------------------------------------------
# attachments.ingest._resolve_parser — the WhatsApp/Gmail seam. Same
# question, different untrusted signal: a sender-declared mime_type instead
# of a filename extension.
# ---------------------------------------------------------------------------

@pytest.fixture
def resolve_parser():
    from app.integrations.attachments.ingest import _resolve_parser
    return _resolve_parser


@pytest.fixture
def mime_to_parser():
    from app.integrations.attachments.ingest import MIME_TO_PARSER
    return MIME_TO_PARSER


class TestAttachmentParserResolution:
    def test_msword_mimetype_and_really_docx_content_agree_no_override_needed(
        self, tmp_path, resolve_parser, mime_to_parser,
    ):
        """`application/msword` already maps to the docx parser here (see
        MIME_TO_PARSER), so real OOXML content under that mime_type needs
        no override — it's a case where the mime-implied and sniffed kind
        happen to agree, and the result must still be the working docx
        parser (not an override, but not a false negative either)."""
        path = tmp_path / "1_quote.doc"
        _write_real_docx(path, "the fitted kitchen quote")

        mime_parser_info = mime_to_parser["application/msword"]
        resolved, reason = resolve_parser(mime_parser_info, path)

        assert resolved is not None
        assert resolved[0] == "docx"
        assert reason is None

    def test_mismatched_mimetype_but_really_docx_content_uses_docx_parser(
        self, tmp_path, resolve_parser, mime_to_parser,
    ):
        """A sender-declared mime_type that implies a *different* office
        format than the real bytes: content sniffing must win rather than
        the label, exactly the class of bug issue #140 is about (the same
        confusion, expressed as a mimetype instead of a filename
        extension)."""
        path = tmp_path / "1_quote.xls"
        _write_real_docx(path, "the fitted kitchen quote")

        mime_parser_info = mime_to_parser["application/vnd.ms-excel"]
        resolved, reason = resolve_parser(mime_parser_info, path)

        assert resolved is not None
        assert resolved[0] == "docx"
        assert reason == "docx"

    def test_pdf_mimetype_and_real_pdf_content_is_unchanged(
        self, tmp_path, resolve_parser, mime_to_parser,
    ):
        path = tmp_path / "1_invoice.pdf"
        path.write_bytes(b"%PDF-1.4\n%mock pdf body")

        mime_parser_info = mime_to_parser["application/pdf"]
        resolved, reason = resolve_parser(mime_parser_info, path)

        assert resolved == mime_parser_info
        assert reason is None

    def test_genuinely_legacy_doc_content_is_reported_not_parsed(
        self, tmp_path, resolve_parser, mime_to_parser,
    ):
        """A real legacy .doc claimed as `application/msword` (an entirely
        truthful mime_type) has no parser either way — must fail with a
        reason naming the real cause, not silently succeed or crash inside
        python-docx."""
        path = tmp_path / "1_old_letter.doc"
        _write_ole2_stub(path)

        mime_parser_info = mime_to_parser["application/msword"]
        resolved, reason = resolve_parser(mime_parser_info, path)

        assert resolved is None
        assert reason == "doc"

    def test_unrecognised_mimetype_but_real_docx_content_still_resolves(
        self, tmp_path, resolve_parser,
    ):
        """No mime-implied parser at all (e.g. `application/octet-stream`),
        but the bytes are real docx content — content sniffing should be
        the primary signal even when the mime_type gave no hint."""
        path = tmp_path / "1_unknown.bin"
        _write_real_docx(path, "the fitted kitchen quote")

        resolved, reason = resolve_parser(None, path)

        assert resolved is not None
        assert resolved[0] == "docx"
        assert reason == "docx"


# ---------------------------------------------------------------------------
# inbox_to_corpus (`handle_to_corpus`) — the third seam. Same question, a
# third untrusted signal: whatever the file happened to be named when it
# landed in the pending queue.
# ---------------------------------------------------------------------------

class _FakeCorpusFacade:
    """Records the path it was actually handed by `ingest_path`, so the test
    can assert on what `handle_to_corpus` renamed the file to before ever
    reaching the corpus dispatcher."""

    def __init__(self):
        self.calls: list = []

    def ingest_path(self, session, path, *, project_tags=None, owner_user_id=None):
        self.calls.append(path)
        return {"document_id": 1, "created": True, "chunks": 1, "embeddings_enqueued": 0}


class TestInboxToCorpusRenamesOnMismatch:
    @pytest.fixture
    def inbox(self, tmp_path, monkeypatch):
        from app.integrations.inbox import scan as scan_mod

        monkeypatch.setattr(scan_mod, "inbox_root", lambda: tmp_path)
        root = tmp_path / "u1" / "incoming"
        root.mkdir(parents=True)
        return root

    def _call(self, session, path_arg, monkeypatch):
        from app.auth.context import use_user
        from app.integrations.inbox import tools as inbox_tools

        fake = _FakeCorpusFacade()
        # `handle_to_corpus` does `from app.plugin.capabilities import
        # get_capability` inline, resolving the name from that module's
        # namespace at call time — patch it there.
        import app.plugin.capabilities as capabilities_mod

        monkeypatch.setattr(capabilities_mod, "get_capability", lambda name: fake)

        with use_user(1):
            result = json.loads(
                inbox_tools.handle_to_corpus(session, {"path": path_arg})
            )
        return result, fake

    def test_doc_that_is_really_docx_is_renamed_before_ingest(self, inbox, monkeypatch):
        path = inbox / "renovation_contract.doc"
        _write_real_docx(path, "the fitted kitchen quote")

        result, fake = self._call(None, str(path), monkeypatch)

        assert result["ok"] is True
        assert len(fake.calls) == 1
        assert fake.calls[0].name == "renovation_contract.docx"
        assert not path.exists()  # renamed (then archived), not left under the old name
        archived = inbox.parent / "archive" / "renovation_contract.docx"
        assert archived.exists()

    def test_extensionless_docx_is_still_named_before_ingest(self, inbox, monkeypatch):
        """Existing behaviour (Tines bare-UUID capture) must survive the
        refactor that generalised this rename to the mismatched-extension
        case too."""
        path = inbox / "3f2a1c9e"
        _write_real_docx(path, "the fitted kitchen quote")

        result, fake = self._call(None, str(path), monkeypatch)

        assert result["ok"] is True
        assert len(fake.calls) == 1
        assert fake.calls[0].name == "3f2a1c9e.docx"

    def test_correctly_named_docx_is_not_touched(self, inbox, monkeypatch):
        path = inbox / "note.docx"
        _write_real_docx(path, "the fitted kitchen quote")

        result, fake = self._call(None, str(path), monkeypatch)

        assert result["ok"] is True
        assert fake.calls[0].name == "note.docx"
