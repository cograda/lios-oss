"""Render the snag register as a PDF for external audiences — the builder
and the engineer (issue #145).

Same source query, grouping and ordering as the two existing exports
(`render.py`'s vault note and `tools.py::_export_to_sheet`'s Sheet): every
snag, ordered `trade, room, id`. The `Snag` model (see `models.py`) carries
no internal/external field split — there is no household-only note column
to strip — so the PDF's column set is the same one already handed to the
Sheet and the vault note; nothing here is newly exposed to an external
reader.

Written into the vault next to `Household/Renovation/Snags.md`, the same
generated-file location the project already uses (evidence photos live in
`Attachments/Snags/` beside it). This codebase has no REST route that
serves an arbitrary generated file — `client_dist.py` serves client wheels
and `attachments`/`media` serve *downloaded* external content, neither of
which fits a server-rendered document — so the vault is the existing
pattern for "a file the household can open", not a new one invented for
this feature. The dashboard (or Obsidian, via Syncthing) opens it from
there.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fpdf import FPDF
from sqlalchemy.orm import Session

from app.integrations.snags.models import Snag
from app.integrations.snags.render import (
    LOCAL_TZ_OFFSET,
    OPEN_STATUSES,
    _ensure_evidence_exported,
)
from app.integrations.snags.vocab import label_for_trade

logger = logging.getLogger(__name__)

SNAGS_PDF_PATH = "Household/Renovation/Snags.pdf"

# Plain-text labels — fpdf2's built-in core fonts (Helvetica) render latin-1
# only, so the emoji badges `render.py` uses for the vault note/Sheet are
# swapped for words in a document handed to a third party.
STATUS_LABELS = {
    "open": "Open",
    "reported": "Reported",
    "accepted": "Accepted",
    "disputed": "Disputed",
    "fixed": "Fixed",
    "verified": "Verified",
    "closed": "Closed",
    "wont-fix": "Won't fix",
}
SEVERITY_LABELS = {"critical": "Critical", "major": "Major", "minor": "Minor", "cosmetic": "Cosmetic"}

_IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
_THUMB_W_MM = 26  # small — a reference for the reader, not a photo library
_MARGIN_MM = 15


def _ascii_safe(text: str) -> str:
    """fpdf2's core fonts can't encode arbitrary Unicode (no embedded TTF
    here — keeping the dependency to fpdf2 alone rather than also shipping a
    font file). Replace anything outside latin-1 rather than raise mid-render;
    a snag description surviving with a `?` in place of a smart quote beats
    the whole export failing."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


class _SnagRegisterPDF(FPDF):
    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, _ascii_safe(f"Page {self.page_no()}"), align="C")


def _snag_block(pdf: _SnagRegisterPDF, snag: Snag, evidence_paths: list) -> None:
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(20, 20, 20)
    location = snag.room + (f" — {snag.element}" if snag.element else "")
    pdf.multi_cell(0, 6, _ascii_safe(f"{snag.uid}  ·  {location}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5.5, _ascii_safe(snag.title), new_x="LMARGIN", new_y="NEXT")
    if snag.description and snag.description != snag.title:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(70, 70, 70)
        pdf.multi_cell(0, 5, _ascii_safe(snag.description), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(50, 50, 50)
    status = STATUS_LABELS.get(snag.status, snag.status)
    severity = SEVERITY_LABELS.get(snag.severity)
    meta_bits = [f"Status: {status}"]
    if severity:
        meta_bits.append(f"Severity: {severity}")
    if snag.reported_at:
        meta_bits.append(f"Reported: {snag.reported_at:%Y-%m-%d}")
    if snag.resolved_at:
        meta_bits.append(f"Resolved: {snag.resolved_at:%Y-%m-%d}")
    if snag.external_ref:
        meta_bits.append(f"Ref: {snag.external_ref}")
    pdf.multi_cell(0, 5, _ascii_safe("  ·  ".join(meta_bits)), new_x="LMARGIN", new_y="NEXT")

    if snag.resolution_note:
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(0, 5, _ascii_safe(f"Note: {snag.resolution_note}"), new_x="LMARGIN", new_y="NEXT")

    if evidence_paths:
        embedded_any = False
        for p in evidence_paths:
            suffix = p.suffix.lower()
            if suffix in _IMAGE_EXTS and p.exists():
                # Trivially reachable — already exported to disk by the vault
                # render step (or this call, via _ensure_evidence_exported).
                # Small thumbnail only; this is a reference, not a gallery.
                y_before = pdf.get_y()
                try:
                    pdf.image(str(p), w=_THUMB_W_MM)
                    embedded_any = True
                except Exception as e:  # pragma: no cover - defensive, e.g. corrupt file
                    logger.warning(f"[snags] pdf: could not embed evidence {p}: {e}")
                    pdf.set_y(y_before)
        non_image = [p.name for p in evidence_paths if p.suffix.lower() not in _IMAGE_EXTS]
        if non_image:
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(90, 90, 90)
            pdf.multi_cell(
                0, 4.5, _ascii_safe("Evidence (see vault Attachments/Snags/): " + ", ".join(non_image)),
                new_x="LMARGIN", new_y="NEXT",
            )
        if embedded_any:
            pdf.ln(1)

    pdf.set_draw_color(220, 220, 220)
    pdf.set_line_width(0.2)
    y = pdf.get_y() + 2
    pdf.line(_MARGIN_MM, y, pdf.w - _MARGIN_MM, y)
    pdf.set_y(y + 3)


def build_snags_pdf(session: Session, *, user_id: int | None = None) -> bytes:
    """Render the register into PDF bytes. Same query/grouping as the vault
    note and Sheet export (`trade, room, id`); evidence photos are exported
    to disk first (idempotent — already-exported files are reused) so they
    are trivially reachable for embedding.
    """
    from app.services.vault_paths import resolve

    evidence_abs = resolve("Attachments/Snags", user_id_override=user_id)

    snags = session.query(Snag).order_by(Snag.trade, Snag.room, Snag.id).all()
    by_trade: dict[str, dict[str, list[Snag]]] = {}
    counts: dict[str, int] = {}
    for s in snags:
        counts[s.status] = counts.get(s.status, 0) + 1
        by_trade.setdefault(s.trade, {}).setdefault(s.room, []).append(s)

    now_local = datetime.now(timezone.utc) + LOCAL_TZ_OFFSET
    open_total = sum(counts.get(st, 0) for st in OPEN_STATUSES)
    done_total = len(snags) - open_total

    pdf = _SnagRegisterPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(_MARGIN_MM, _MARGIN_MM, _MARGIN_MM)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(15, 15, 15)
    pdf.cell(0, 10, "Snag Register", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, _ascii_safe(f"Generated {now_local:%Y-%m-%d %H:%M}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(
        0, 6,
        _ascii_safe(f"{len(snags)} snags  ·  {open_total} open  ·  {done_total} resolved/closed"),
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(2)

    # Status legend — the audience is external (builder/engineer), so the
    # lifecycle vocabulary is spelled out rather than assumed.
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 5, "Status legend:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    legend = "  ·  ".join(f"{v}" for v in STATUS_LABELS.values())
    pdf.multi_cell(0, 5, _ascii_safe(legend), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    for trade in sorted(by_trade, key=lambda t: (t == "unknown", t)):
        rooms = by_trade[trade]
        trade_open = sum(1 for room in rooms.values() for s in room if s.status in OPEN_STATUSES)
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(15, 15, 15)
        pdf.cell(0, 9, _ascii_safe(f"{label_for_trade(trade)} ({trade_open} open)"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

        for room in sorted(rooms):
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(40, 40, 40)
            pdf.cell(0, 7, _ascii_safe(room), new_x="LMARGIN", new_y="NEXT")
            for s in rooms[room]:
                links = _ensure_evidence_exported(session, s, evidence_abs)
                evidence_paths = [evidence_abs / p.split("/")[-1] for p in links]
                _snag_block(pdf, s, evidence_paths)
        pdf.ln(2)

    session.commit()  # persist any vault_path set during evidence export
    return bytes(pdf.output())


def render_snags_pdf(session: Session, user_id: int | None = None) -> str:
    """Render the register to PDF and write it into the vault next to the
    generated markdown note. Returns the vault-relative path."""
    from app.services.vault_paths import resolve

    pdf_abs = resolve(SNAGS_PDF_PATH, user_id_override=user_id)
    pdf_bytes = build_snags_pdf(session, user_id=user_id)
    pdf_abs.parent.mkdir(parents=True, exist_ok=True)
    pdf_abs.write_bytes(pdf_bytes)
    logger.info(f"[snags] rendered PDF -> {SNAGS_PDF_PATH} ({len(pdf_bytes)} bytes)")
    return SNAGS_PDF_PATH
