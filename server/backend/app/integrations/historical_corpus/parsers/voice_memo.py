"""Parse voice-memo transcript markdown into the historical corpus.

Source files are the keeper transcripts published by the `sandbox/voice-memos`
pipeline (`publish_vault.py`) — markdown with YAML frontmatter
(id/date/source/topics/people) followed by a body of: an `# {id} · {title}`
heading, a few metadata bullets, a `**Summary:**`, optional `**Action items:**`,
a `---` separator, then the raw transcript.

Two chunk kinds are emitted:
  - one `voice_memo_summary` chunk (title + summary + action items — the distilled,
    high-signal content), and
  - N `voice_memo` chunks of the raw transcript.

Transcripts are frequently a single unbroken paragraph (Apple's transcriber emits
no blank lines), so we word-window the transcript directly rather than relying on
the paragraph-based `_chunk_text` — otherwise a 4,000-word memo would land as one
chunk and the embedder (bge-small, ~512 token cap) would only see the head.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta

logger = logging.getLogger(__name__)

WORDS_PER_CHUNK = 350
WORD_OVERLAP = 50


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split leading `---`-delimited YAML frontmatter from the body.

    Returns (frontmatter_dict, body). Only the shallow key: value shape the
    publisher emits is handled (scalars + simple `["a", "b"]` lists)."""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    fm: dict = {}
    for line in raw.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            items = [p.strip().strip('"').strip("'") for p in inner.split(",")] if inner else []
            fm[key] = [i for i in items if i]
        else:
            fm[key] = val.strip('"').strip("'")
    return fm, body


def _word_windows(text: str, size: int = WORDS_PER_CHUNK, overlap: int = WORD_OVERLAP) -> list[str]:
    """Split text into overlapping word windows so each chunk stays embeddable."""
    words = text.split()
    if not words:
        return []
    if len(words) <= size:
        return [text.strip()]
    step = max(1, size - overlap)
    out: list[str] = []
    for start in range(0, len(words), step):
        out.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
    return out


def _split_body(body: str) -> tuple[str, str]:
    """Return (header, transcript). Header = heading/summary/action-items block;
    transcript = the text after the final `---` separator."""
    # The publisher writes "...action items...\n\n---\n\n<transcript>".
    parts = re.split(r"\n-{3,}\n", body)
    if len(parts) >= 2:
        header = parts[0].strip()
        transcript = "\n".join(parts[1:]).strip()
    else:
        header, transcript = "", body.strip()
    return header, transcript


def _title(header: str, fm: dict, path: Path) -> str:
    m = re.search(r"^#\s+(.+)$", header, re.MULTILINE)
    if m:
        t = m.group(1).strip()
        # "VM104 · Riverside Requests for Cameron" → keep the descriptive part.
        return t.split("·", 1)[1].strip() if "·" in t else t
    return fm.get("id") or path.stem


def _doc_date(fm: dict) -> date | None:
    raw = (fm.get("date") or "")[:10]
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def parse(path: Path) -> tuple[DocMeta, list[ChunkRecord]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, body = _parse_frontmatter(text)
    header, transcript = _split_body(body)

    vm_id = fm.get("id") or path.stem.split(" ", 1)[0]
    title = _title(header, fm, path)
    topics = fm.get("topics") if isinstance(fm.get("topics"), list) else []
    people = fm.get("people") if isinstance(fm.get("people"), list) else []

    meta = DocMeta(
        title=title,
        source_type="voice_memo",
        author=None,
        participants=people,
        document_date=_doc_date(fm),
        metadata={
            "vm_id": vm_id,
            "topics": topics,
            "transcript_source": fm.get("source") or "",
            "word_count": len(transcript.split()),
        },
    )

    breadcrumb = f"Voice Memo › {vm_id} · {title}"
    chunks: list[ChunkRecord] = []

    # High-signal summary chunk first (title + summary + action items).
    if header:
        chunks.append(ChunkRecord(
            chunk_type="voice_memo_summary",
            chunk_text=header,
            breadcrumb=breadcrumb,
            metadata={"vm_id": vm_id, "chunk_index_within_doc": 0},
        ))

    # Then the transcript, word-windowed.
    for i, win in enumerate(_word_windows(transcript)):
        chunks.append(ChunkRecord(
            chunk_type="voice_memo",
            chunk_text=win,
            breadcrumb=breadcrumb,
            metadata={"vm_id": vm_id, "chunk_index_within_doc": len(chunks)},
        ))

    return meta, chunks
