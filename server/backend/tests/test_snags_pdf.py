"""Snag register PDF export (issue #145) — unit tier.

Builds the PDF against a mocked session and a handful of in-memory `Snag`
rows (never added to any session, so no DB is required — `vault_paths.resolve`
and evidence export are monkeypatched out the same way, since both would
otherwise need a real `users` table lookup). Content is verified with pypdf
(already a project dependency, used elsewhere for *reading* PDFs), checking
both that the file is structurally a real multi-object PDF and that the
snag text actually made it onto a page.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.integrations.snags.models import Snag


def _fake_snag(**kw) -> Snag:
    defaults = dict(
        id=1,
        uid="SNAG-0001",
        title="Cracked tile",
        description="Cracked tile",
        room="Kitchen",
        element=None,
        trade="tiler",
        severity="major",
        status="open",
        reported_by="alex",
        reported_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        source_ref=None,
        reported_to_trade_at=None,
        external_ref=None,
        resolution_note=None,
        resolved_at=None,
    )
    defaults.update(kw)
    return Snag(**defaults)


def _mock_session(snags: list[Snag]) -> MagicMock:
    session = MagicMock()
    q = MagicMock()
    q.order_by.return_value.all.return_value = snags
    session.query.return_value = q
    return session


@pytest.fixture(autouse=True)
def _no_vault_no_evidence(tmp_path, monkeypatch):
    """Keep this a DB-free unit test: `build_snags_pdf`/`render_snags_pdf`
    resolve vault paths (a real `users` table lookup), read deployment
    config for trade display labels (`integration_config`), and export
    evidence photos (a `snag_media`/`media_items` join) — none of that is
    what this test is exercising, so all three are stubbed to plain,
    DB-free behaviour (mirrors `test_snags_sheet_export.py`'s approach for
    `plugin_config`)."""
    from types import SimpleNamespace

    import app.integrations.snags.pdf as pdf_mod
    import app.integrations.snags.vocab as vocab_mod
    import app.services.vault_paths as vault_paths

    monkeypatch.setattr(vault_paths, "resolve", lambda path, user_id_override=None: tmp_path / path)
    monkeypatch.setattr(pdf_mod, "_ensure_evidence_exported", lambda session, snag, evidence_abs: [])
    monkeypatch.setattr(
        vocab_mod, "plugin_config",
        lambda name: SimpleNamespace(trades=[], trade_labels={}, room_aliases={}),
    )


class TestBuildSnagsPdf:
    def test_produces_a_real_pdf(self):
        from app.integrations.snags.pdf import build_snags_pdf

        session = _mock_session([_fake_snag()])
        pdf_bytes = build_snags_pdf(session)

        assert pdf_bytes.startswith(b"%PDF-")
        assert pdf_bytes.rstrip().endswith(b"%%EOF")
        # A one-page, one-snag PDF from fpdf2 is comfortably over 1KB —
        # "non-trivial" here rules out an empty/near-empty document.
        assert len(pdf_bytes) > 1200
        session.commit.assert_called()

    def test_snag_text_is_actually_on_the_page(self):
        pypdf = pytest.importorskip("pypdf")
        from app.integrations.snags.pdf import build_snags_pdf

        snags = [
            _fake_snag(
                id=1, uid="SNAG-0001", title="Cracked tile in the utility room",
                description="Corner tile cracked during delivery, needs replacing",
                room="Utility Room", trade="tiler", status="open", severity="major",
            ),
            _fake_snag(
                id=2, uid="SNAG-0002", title="Dripping tap", description="Dripping tap",
                room="Bathroom", trade="plumber", status="fixed", severity="cosmetic",
                resolution_note="Washer replaced 2026-08-20",
            ),
        ]
        session = _mock_session(snags)
        pdf_bytes = build_snags_pdf(session)

        reader = pypdf.PdfReader(__import__("io").BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)

        assert "Snag Register" in text
        assert "SNAG-0001" in text and "SNAG-0002" in text
        assert "Cracked tile in the utility room" in text
        assert "Corner tile cracked during delivery" in text
        assert "Washer replaced" in text
        # Status legend + grouping headers
        assert "Status legend" in text
        assert "Tiler" in text or "tiler" in text.lower()

    def test_groups_by_trade_then_room_like_the_other_exports(self):
        pypdf = pytest.importorskip("pypdf")
        from app.integrations.snags.pdf import build_snags_pdf

        snags = [
            _fake_snag(id=1, uid="SNAG-0001", room="Kitchen", trade="electrician"),
            _fake_snag(id=2, uid="SNAG-0002", room="Attic", trade="electrician"),
            _fake_snag(id=3, uid="SNAG-0003", room="Hall", trade="carpenter"),
        ]
        session = _mock_session(snags)
        pdf_bytes = build_snags_pdf(session)

        reader = pypdf.PdfReader(__import__("io").BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)

        # Trades sort alphabetically (same as render.py's `by_trade` sort),
        # so carpenter's Hall comes before electrician's Attic/Kitchen; rooms
        # sort alphabetically within a trade, so Attic precedes Kitchen.
        attic_idx = text.find("Attic")
        kitchen_idx = text.find("Kitchen")
        hall_idx = text.find("Hall")
        assert -1 not in (attic_idx, kitchen_idx, hall_idx)
        assert hall_idx < attic_idx     # carpenter's block before electrician's
        assert attic_idx < kitchen_idx  # Attic before Kitchen, alphabetical within a trade

    def test_non_ascii_text_does_not_crash_the_render(self):
        """fpdf2 core fonts are latin-1 only — content outside that range must
        be degraded, not raise, mid-export."""
        from app.integrations.snags.pdf import build_snags_pdf

        session = _mock_session([_fake_snag(description="Snagged — café tiles \U0001F6E0")])
        pdf_bytes = build_snags_pdf(session)
        assert pdf_bytes.startswith(b"%PDF-")


class TestRenderSnagsPdf:
    def test_writes_the_file_and_returns_the_vault_relative_path(self, tmp_path):
        from app.integrations.snags.pdf import SNAGS_PDF_PATH, render_snags_pdf

        session = _mock_session([_fake_snag()])
        result = render_snags_pdf(session)

        assert result == SNAGS_PDF_PATH == "Household/Renovation/Snags.pdf"
        written = tmp_path / SNAGS_PDF_PATH
        assert written.exists()
        assert written.read_bytes().startswith(b"%PDF-")


class TestSnagExportPdfTool:
    def test_registered_with_correct_annotations(self):
        from app.integrations.snags.tools import mcp_tools

        tool = next(t for t in mcp_tools() if t["name"] == "snag_export_pdf")
        assert tool["annotations"]["readOnlyHint"] is False
        assert tool["annotations"]["idempotentHint"] is True
        assert tool["inputSchema"] == {"type": "object", "properties": {}}

    def test_handler_renders_and_reports_the_path(self, monkeypatch):
        import app.integrations.snags.tools as snag_tools

        monkeypatch.setattr(snag_tools, "render_snags_pdf", lambda session: "Household/Renovation/Snags.pdf")
        out = snag_tools.snag_export_pdf_handler(MagicMock(), {})

        import json
        assert json.loads(out) == {"rendered": "Household/Renovation/Snags.pdf"}
