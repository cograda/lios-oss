"""Inbox enrichment + queue walking.

Two responsibilities, both filesystem-only:

  1. `list_pending()` — walk the pending buckets and return one record per
     file (file + its `.meta.json` sidecar merged into a single dict).
  2. `enrich_pending(session)` — for each file lacking `enriched_at` in its
     sidecar, sniff its kind and extract a short preview, then write the
     enriched fields back to the sidecar. Idempotent.

The Postgres session is accepted but currently unused — kept on the signature
so we can move enrichment artefacts into a DB table later without changing
the scheduler's call site.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import HomeSettings

logger = logging.getLogger(__name__)

settings = HomeSettings()

# Subdirs under /inbox/ that count as "pending". `archive` and `dismissed`
# are terminal states and skipped.
PENDING_BUCKETS = {"incoming", "text", "image", "audio", "file"}
TERMINAL_BUCKETS = {"archive", "dismissed"}

PREVIEW_CHARS = 500  # how much text to capture in the sidecar preview

# Magic-byte sniffer. Cheap, no deps. Extend as needed.
_MAGIC = [
    (b"%PDF",          "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff",  "image"),  # JPEG
    (b"GIF8",          "image"),
    (b"RIFF",          "audio"),  # WAV/AVI/WebP — close enough for triage
    (b"ID3",           "audio"),  # MP3 with ID3 tag
    (b"\xff\xfb",      "audio"),  # MP3 frame
    (b"\x00\x00\x00 ftyp", "video"),  # MP4-family (also m4a — see below)
    (b"OggS",          "audio"),
    (b"PK\x03\x04",    "zip"),    # also docx/xlsx — caller can refine
]

# Extension hints used when magic-byte sniff is ambiguous (text-ish files
# don't have a useful magic number) or when the magic sniff picks the
# wrong family (e.g. .m4a is technically ISO-BMFF/ftyp).
_EXT_KIND = {
    ".pdf":  "pdf",
    ".txt":  "text",
    ".md":   "markdown",
    ".markdown": "markdown",
    ".json": "text",
    ".csv":  "text",
    ".log":  "text",
    ".png":  "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".heic": "image", ".webp": "image",
    ".m4a":  "audio", ".mp3": "audio", ".wav": "audio", ".aac": "audio",
    ".flac": "audio", ".ogg": "audio",
    ".mp4":  "video", ".mov": "video", ".webm": "video",
    ".docx": "docx",
    ".xlsx": "xlsx",
}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def inbox_root() -> Path:
    return Path(settings.inbox_path)


def safe_resolve(rel_or_abs: str) -> Path:
    """Resolve a caller-supplied path and confirm it stays under /inbox/.

    Accepts either an absolute /inbox/... path (what tool calls usually pass)
    or a bucket-relative path. Raises ValueError if it escapes the root.
    """
    root = inbox_root().resolve()
    p = Path(rel_or_abs)
    full = (p if p.is_absolute() else root / p).resolve()
    if root not in full.parents and full != root:
        raise ValueError(f"path escapes inbox root: {rel_or_abs}")
    return full


def sidecar_path(file_path: Path) -> Path:
    """Sidecar lives next to the file with `.meta.json` appended (full
    suffix preserved so `foo.pdf` → `foo.pdf.meta.json`, not `foo.meta.json`)."""
    return file_path.with_suffix(file_path.suffix + ".meta.json")


def _is_sidecar(p: Path) -> bool:
    return p.name.endswith(".meta.json")


# ---------------------------------------------------------------------------
# Kind sniffer + preview extractor
# ---------------------------------------------------------------------------


def sniff_kind(path: Path) -> str:
    """Return a short kind label: pdf | text | markdown | image | audio |
    video | docx | xlsx | zip | unknown."""
    suffix = path.suffix.lower()
    if suffix in _EXT_KIND:
        return _EXT_KIND[suffix]
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return "unknown"
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    # Heuristic: if it's all printable ASCII/UTF-8, call it text.
    if head and all(b == 9 or b == 10 or b == 13 or 32 <= b < 127 for b in head):
        return "text"
    return "unknown"


def extract_preview(path: Path, kind: str) -> tuple[str, dict[str, Any]]:
    """Return (preview, extra_metadata). Preview is capped at PREVIEW_CHARS.

    `extra_metadata` carries kind-specific facts (page count, line count,
    image dimensions) that the skill can show without re-reading the file.
    """
    try:
        if kind == "pdf":
            return _preview_pdf(path)
        if kind in ("text", "markdown"):
            return _preview_text(path)
        if kind == "image":
            return _preview_image(path)
        if kind == "audio":
            return "", {"note": "audio preview not implemented in V1"}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[inbox] preview failed for {path.name}: {e}")
        return "", {"preview_error": str(e)[:200]}
    return "", {}


def _preview_pdf(path: Path) -> tuple[str, dict[str, Any]]:
    """First page's text (capped). Uses pypdf — same library the corpus
    parser starts with — so we don't load pdfplumber for triage."""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    n_pages = len(reader.pages)
    first = ""
    if n_pages:
        try:
            first = (reader.pages[0].extract_text() or "").strip()
        except Exception as e:  # noqa: BLE001
            first = f"(extract failed: {e})"
    return first[:PREVIEW_CHARS], {"page_count": n_pages}


def _preview_text(path: Path) -> tuple[str, dict[str, Any]]:
    """Read up to PREVIEW_CHARS. Don't blow memory on huge files."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return "", {"preview_error": str(e)}
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    return text[:PREVIEW_CHARS], {"line_count": lines, "char_count": len(text)}


def _preview_image(path: Path) -> tuple[str, dict[str, Any]]:
    """Dimensions only. Pillow is already pulled in by other deps; if not
    present, fall back to size-on-disk."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return "", {"width": img.width, "height": img.height, "format": img.format}
    except Exception:
        return "", {"note": "image dimensions unavailable"}


# ---------------------------------------------------------------------------
# Sidecar I/O
# ---------------------------------------------------------------------------


def read_sidecar(file_path: Path) -> dict[str, Any]:
    sc = sidecar_path(file_path)
    if not sc.exists():
        return {}
    try:
        return json.loads(sc.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[inbox] bad sidecar {sc.name}: {e}")
        return {}


def write_sidecar(file_path: Path, data: dict[str, Any]) -> None:
    sc = sidecar_path(file_path)
    sc.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def iter_pending_files() -> list[Path]:
    """All non-sidecar files in any pending bucket. Sorted oldest first so
    triage tackles backlog in arrival order."""
    root = inbox_root()
    out: list[Path] = []
    for bucket in PENDING_BUCKETS:
        d = root / bucket
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and not _is_sidecar(p):
                out.append(p)
    out.sort(key=lambda p: p.name)  # timestamp-prefixed → chronological
    return out


def count_pending() -> int:
    return len(iter_pending_files())


def enrich_one(file_path: Path, *, force: bool = False) -> dict[str, Any]:
    """Sniff + preview a single file, persist into its sidecar. Returns the
    full updated sidecar dict so callers (tests, the on-demand `inbox_pending`
    tool) can use it without re-reading from disk."""
    meta = read_sidecar(file_path)
    if meta.get("enriched_at") and not force:
        return meta

    kind = sniff_kind(file_path)
    preview, extras = extract_preview(file_path, kind)
    meta.setdefault("ingested_at", None)
    meta["kind"] = kind
    meta["preview"] = preview
    meta["preview_meta"] = extras
    meta["enriched_at"] = datetime.now(timezone.utc).isoformat()
    write_sidecar(file_path, meta)
    return meta


def enrich_pending(session: Session | None = None) -> dict[str, int]:
    """Enrich every pending file whose sidecar doesn't already have
    `enriched_at`. Cheap to re-run."""
    pending = iter_pending_files()
    enriched = 0
    for p in pending:
        sc = read_sidecar(p)
        if sc.get("enriched_at"):
            continue
        try:
            enrich_one(p)
            enriched += 1
        except Exception:  # noqa: BLE001
            logger.exception(f"[inbox] enrich failed: {p}")
    logger.info(f"[inbox] enriched {enriched}/{len(pending)} pending files")
    return {"pending": len(pending), "enriched": enriched}


def list_pending(limit: int = 50) -> list[dict[str, Any]]:
    """One dict per pending file, sidecar merged in, suitable for the
    `inbox_pending` MCP tool."""
    root = inbox_root().resolve()
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for p in iter_pending_files()[:limit]:
        meta = read_sidecar(p)
        st = p.stat()
        try:
            bucket = p.parent.relative_to(root).parts[0]
        except (ValueError, IndexError):
            bucket = "?"
        ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        out.append({
            "path": str(p),
            "filename": p.name,
            "original_filename": meta.get("original_filename"),
            "bucket": bucket,
            "size_bytes": st.st_size,
            "age_minutes": int((now - ts).total_seconds() // 60),
            "kind": meta.get("kind") or sniff_kind(p),
            "preview": meta.get("preview", ""),
            "preview_meta": meta.get("preview_meta", {}),
            "type_hint": meta.get("type_hint"),
            "source": meta.get("source"),
            "extra": meta.get("extra"),
            "enriched": bool(meta.get("enriched_at")),
        })
    return out


# ---------------------------------------------------------------------------
# State transitions (used by the routing tools)
# ---------------------------------------------------------------------------


def move_to(file_path: Path, terminal: str) -> Path:
    """Move file + sidecar into /inbox/<terminal>/. Terminal must be
    'archive' or 'dismissed'."""
    if terminal not in TERMINAL_BUCKETS:
        raise ValueError(f"invalid terminal bucket: {terminal}")
    dest_dir = inbox_root() / terminal
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file_path.name
    # If a same-named file already lives there (re-routed twice), suffix it.
    if dest.exists():
        stem = file_path.stem
        suffix = file_path.suffix
        i = 1
        while True:
            candidate = dest_dir / f"{stem}-{i}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
            i += 1
    shutil.move(str(file_path), str(dest))
    sc = sidecar_path(file_path)
    if sc.exists():
        shutil.move(str(sc), str(sidecar_path(dest)))
    return dest
