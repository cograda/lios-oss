"""Parse PDFs via pypdf with a pdfplumber fallback for low-yield extraction.

Strategy:
  1. pypdf.PdfReader — fast, works on native/digital PDFs.
  2. If total text < 100 chars (likely scanned), retry with pdfplumber.
  3. If still empty, flag metadata.extraction_quality='low' and index the
     filename + breadcrumb only. OCR is deferred (plan doc §Non-goals).

Chunking: paragraph-aware, ~2000 chars/chunk with 200-char overlap, split
on blank-line boundaries where possible so contract clauses stay whole.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta

logger = logging.getLogger(__name__)

CHUNK_TARGET_CHARS = 2000
CHUNK_OVERLAP_CHARS = 200
LOW_YIELD_THRESHOLD = 100


# PDF extractors (pypdf and pdfplumber) emit a newline after every visual line,
# not every paragraph. They also keep column-alignment padding as literal runs
# of spaces/tabs. Left alone, this breaks embedding token boundaries and hurts
# retrieval. These regexes collapse the noise without touching semantic content.
_RE_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
# Soft line-wrap: lowercase/punct + newline + lowercase/digit = mid-sentence wrap.
# Capitals aren't joined — might be a real new sentence or heading.
_RE_SOFT_WRAP = re.compile(r"([a-z,;:])\n([a-z0-9])")
# End-of-line hyphenation: "thermal-\nconductivity" → "thermalconductivity".
_RE_HYPHEN_WRAP = re.compile(r"(\w)-\n(\w)")


def _normalise(text: str) -> str:
    """Clean whitespace artefacts without losing content.

    Applied after extraction, before chunking, so embeddings see sane paragraph
    structure and the chunker's blank-line split actually maps to paragraphs.
    """
    if not text:
        return text
    text = _RE_TRAILING_WS.sub("", text)
    text = _RE_HYPHEN_WRAP.sub(r"\1\2", text)
    text = _RE_SOFT_WRAP.sub(r"\1 \2", text)
    text = _RE_MULTI_SPACE.sub(" ", text)
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def _extract_pypdf(path: Path) -> tuple[str, dict]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    info = reader.metadata or {}
    texts: list[str] = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception as e:  # noqa: BLE001 — pypdf raises grab-bag of errors
            logger.warning(f"pypdf page extract failed for {path.name}: {e}")
            texts.append("")
    body = "\n\n".join(t.strip() for t in texts if t.strip())
    doc_meta: dict = {
        "page_count": len(reader.pages),
        "pdf_author": str(info.get("/Author", "")) if info else "",
        "pdf_title": str(info.get("/Title", "")) if info else "",
        "pdf_created": str(info.get("/CreationDate", "")) if info else "",
    }
    return body, doc_meta


def _extract_pdfplumber(path: Path) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t.strip())
    return "\n\n".join(parts)


def _chunk_text(text: str) -> list[str]:
    if len(text) <= CHUNK_TARGET_CHARS:
        return [text] if text else []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        if not buf:
            buf = para
            continue
        if len(buf) + 2 + len(para) <= CHUNK_TARGET_CHARS:
            buf += "\n\n" + para
        else:
            chunks.append(buf)
            # Carry tail of previous chunk for overlap continuity.
            tail = buf[-CHUNK_OVERLAP_CHARS:] if len(buf) > CHUNK_OVERLAP_CHARS else ""
            buf = (tail + "\n\n" + para) if tail else para
    if buf:
        chunks.append(buf)
    return chunks


def _parse_creation_date(raw: str):
    # PDF dates look like "D:20240119101322+00'00'"
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def parse(path: Path) -> tuple[DocMeta, list[ChunkRecord]]:
    breadcrumb_stem = path.parent.name
    title = path.stem

    try:
        body, info = _extract_pypdf(path)
    except Exception as e:
        logger.warning(f"pypdf failed outright on {path}: {e}")
        body, info = "", {"page_count": 0}

    quality = "ok"
    if len(body) < LOW_YIELD_THRESHOLD:
        try:
            alt = _extract_pdfplumber(path)
            if len(alt) > len(body):
                body = alt
        except Exception as e:
            logger.warning(f"pdfplumber fallback failed on {path}: {e}")
        if len(body) < LOW_YIELD_THRESHOLD:
            quality = "low"

    body = _normalise(body)

    meta = DocMeta(
        title=info.get("pdf_title") or title,
        source_type="pdf",
        author=info.get("pdf_author") or None,
        document_date=_parse_creation_date(info.get("pdf_created", "")),
        metadata={
            **info,
            "extraction_quality": quality,
        },
    )

    chunks: list[ChunkRecord] = []
    if quality == "low":
        # Index the filename + folder so the file is at least findable by name.
        chunks.append(ChunkRecord(
            chunk_type="pdf_low_yield",
            chunk_text=f"PDF: {title}\nFolder: {breadcrumb_stem}\n(Scanned or image-only — OCR deferred)",
            breadcrumb=f"PDF › {breadcrumb_stem} › {title}",
            metadata={"extraction_quality": "low"},
        ))
        return meta, chunks

    for i, chunk_text in enumerate(_chunk_text(body)):
        chunks.append(ChunkRecord(
            chunk_type="pdf_page_chunk",
            chunk_text=chunk_text,
            breadcrumb=f"PDF › {breadcrumb_stem} › {title}",
            metadata={"chunk_index_within_doc": i},
        ))
    return meta, chunks
