"""Parse equipment manuals into the historical corpus.

Source files are the normalised markdown produced by
`Documents/Reference/Manuals/_tools/build_text.py` — one file per manual, with YAML
frontmatter (title/manual_of/kind/source_file/extraction/language/pages_*) followed by the
manual body.

## Why this parser exists rather than reusing `pdf`

The manuals are markdown, not PDF, and that is deliberate. Three problems have to be solved
before a manual's text is worth embedding — missing word-spacing in some vendor PDFs, scanned
pages needing OCR, and single files containing the same manual in up to ten languages — and
all three are solved in the build step, which runs where poppler and tesseract live. By the
time a file reaches this parser it is clean English prose, so the parser's whole job is to
read frontmatter and chunk.

## Why not the `voice_memo` parser, which also reads .md

Before this existed, `_dispatch_by_suffix` sent *every* `.md` file to `voice_memo`. A manual
ingested that way is not merely mislabelled: it acquires `source_type="voice_memo"`, gets
chunked by the transcript word-windower, and its frontmatter is read for memo fields that do
not exist. A search for "heat pump defrost" would then surface what looks like a spoken note.
The `manual_of` frontmatter key is what distinguishes the two.

Chunking is paragraph-accumulating with overlap, matching `pdf.py` — manuals are structured
prose with headings, so paragraph boundaries are real, unlike the unbroken transcripts
`voice_memo` has to word-window.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta

logger = logging.getLogger(__name__)

CHUNK_TARGET_CHARS = 1800
CHUNK_OVERLAP_CHARS = 200

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
FM_LINE = re.compile(r'^([a-z_]+):\s*(.*)$')


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split leading `---` YAML frontmatter. Only flat `key: value` is supported, which is
    all the builder emits; values may be double-quoted."""
    if not text.startswith("---"):
        return {}, text
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        lm = FM_LINE.match(line.strip())
        if not lm:
            continue
        key, val = lm.group(1), lm.group(2).strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        fm[key] = val
    return fm, m.group(2)


def looks_like_manual(path: Path) -> bool:
    """Cheap sniff used by the ingest dispatcher — reads only the frontmatter block.

    Keyed on content rather than directory so a manual is routed correctly wherever it sits,
    including a one-off `ingest_path` on a file outside the manuals tree.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(1024)
    except OSError:
        return False
    return head.startswith("---") and "\nmanual_of:" in head


def _chunk_text(body: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
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
            tail = buf[-CHUNK_OVERLAP_CHARS:] if len(buf) > CHUNK_OVERLAP_CHARS else ""
            buf = (tail + "\n\n" + para) if tail else para
    if buf:
        chunks.append(buf)
    return chunks


def _built_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse(path: Path) -> tuple[DocMeta, list[ChunkRecord]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, body = _parse_frontmatter(text)

    title = fm.get("title") or path.stem
    device = fm.get("manual_of") or title
    kind = fm.get("kind") or "manual"

    # Strip the duplicated H1 the builder writes — it repeats the title, and as the first
    # chunk's opening line it would compete with real content for the same query.
    body = re.sub(r"^\s*#\s+.*\n+", "", body, count=1)

    meta = DocMeta(
        title=title,
        source_type="manual",
        document_date=_built_date(fm.get("built", "")),
        metadata={
            "manual_of": device,
            "kind": kind,
            "source_file": fm.get("source_file"),
            "source_url": fm.get("source_url") or None,
            "extraction": fm.get("extraction"),
            "language": fm.get("language", "en"),
            "pages_total": fm.get("pages_total"),
            "pages_kept": fm.get("pages_kept"),
            "multilingual_source": fm.get("multilingual_source") == "true",
        },
    )

    breadcrumb = f"Manual › {device} › {kind}"
    chunks = [
        ChunkRecord(
            chunk_type="manual_chunk",
            # Prefixing each chunk with the device keeps retrieval anchored: manual prose is
            # full of unqualified pronouns ("the unit", "this appliance"), so a mid-document
            # chunk on its own gives the embedder nothing to tie it to the equipment.
            chunk_text=f"{device} — {kind}\n\n{c}",
            breadcrumb=breadcrumb,
            metadata={"chunk_index_within_doc": i, "manual_of": device},
        )
        for i, c in enumerate(_chunk_text(body))
    ]
    return meta, chunks
