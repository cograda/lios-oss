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
import os
import re
import shutil
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import HomeSettings

logger = logging.getLogger(__name__)

settings = HomeSettings()

# Subdirs under /inbox/ that count as "pending". `archive` and `dismissed`
# are terminal states and skipped.
PENDING_BUCKETS = {"incoming", "text", "image", "audio", "file"}
TERMINAL_BUCKETS = {"archive", "dismissed"}

# How many transient-failure sweeps `transcribe_pending` tolerates before it
# gives up on a file for good, matching the retired Tines story's `retries: 3`.
MAX_TRANSCRIPTION_ATTEMPTS = 3

# How long a `*.transcribing.lock` file (see `_acquire_transcription_lock`)
# is honoured before it's treated as orphaned by a crashed process and
# reclaimed. Comfortably longer than any real transcription call — including
# a long meeting recording — so this only ever fires on a genuine crash, not
# on a slow-but-healthy call.
_TRANSCRIPTION_LOCK_STALE_SECONDS = 15 * 60

# Suffix appended to a captured file to claim it for transcription. Kept as a
# constant because `_is_internal_artifact` must recognise exactly what
# `_transcription_lock_path` writes — a drift between the two would put lock
# files back in the user's inbox.
_TRANSCRIPTION_LOCK_SUFFIX = ".transcribing.lock"

# F6 (2026-08-08): the inbox is per-user. Every user's items live under
# `<inbox_root>/u<user_id>/<bucket>/...`; files still sitting directly under
# `<inbox_root>/<bucket>/...` are "legacy flat-tree" — ingested before this
# split — and are lazily adopted into `LEGACY_OWNER_USER_ID`'s subtree by
# `adopt_legacy_files()`. Alex (user 1) is the only person who has ever used
# this integration (Sam's onboarding predates none of her captures landing
# here), matching every other "existing rows belong to user 1" migration in
# this codebase (see e.g. `alembic/versions/*_vault_chunks_user_scope.py`).
LEGACY_OWNER_USER_ID = 1

_USER_DIR_RE = re.compile(r"^u(\d+)$")

PREVIEW_CHARS = 500  # how much text to capture in the sidecar preview

# How much of a file's head to read when sniffing its kind. 16 bytes is enough
# for every magic number here, but not to find an HTML marker that sits behind a
# BOM, a licence comment, or a conditional-comment block.
_SNIFF_BYTES = 2048

# Magic-byte sniffer. Cheap, no deps. Extend as needed.
#
# NB the ISO-BMFF (MP4/M4A/MOV) family is deliberately NOT in this list — see
# `_sniff_ftyp_brand()`. It used to be here as `b"\x00\x00\x00 ftyp"`, which
# could only ever match a file whose ftyp box happened to be exactly 32 bytes
# long, because the first four bytes are the box *length*, not a constant. Any
# other length fell through to "unknown" — which is how iOS voice notes
# (arriving from Tines with no extension to fall back on) ended up unclassified.
_MAGIC = [
    (b"%PDF",          "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff",  "image"),  # JPEG
    (b"GIF8",          "image"),
    (b"RIFF",          "audio"),  # WAV/AVI/WebP — close enough for triage
    (b"ID3",           "audio"),  # MP3 with ID3 tag
    (b"\xff\xfb",      "audio"),  # MP3 frame
    (b"OggS",          "audio"),
    (b"PK\x03\x04",    "zip"),    # also docx/xlsx — caller can refine
]

# ISO-BMFF major brands (bytes 8:12, right after "ftyp") that mean audio rather
# than video. `M4A ` is what iOS Voice Memos and the Files app produce; `M4B` is
# an audiobook; `mp42`/`isom` are ambiguous containers and stay video, which is
# the safer default (a video preview costs nothing, a mis-labelled audio note
# would silently skip transcription later).
_FTYP_AUDIO_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B "}

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
    ".html": "html", ".htm": "html",
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


def user_root(user_id: int) -> Path:
    """Filesystem root of one user's inbox subtree.

    Keyed by bare user id (`u1`, `u2`, ...) rather than the user's name —
    unlike `vault_paths.user_vault_path`, nothing here needs a human-readable
    directory name, and keying by id avoids a DB round-trip (a lookup of
    `users.name`) on every inbox tool call.
    """
    return inbox_root() / f"u{user_id}"


def safe_resolve(rel_or_abs: str, user_id: int) -> Path:
    """Resolve a caller-supplied path and confirm it stays under the calling
    user's own inbox subtree — never any other user's, and never the flat
    legacy tree directly.

    Accepts either an absolute path (what tool calls usually pass, since
    `list_pending` returns absolute paths) or a bucket-relative path. Raises
    ValueError if it escapes the caller's root — this is the enforcement
    point that stops user 2 from passing user 1's absolute path into
    `inbox_preview`/`inbox_archive`/etc. and reading or moving their file.
    """
    root = user_root(user_id).resolve()
    p = Path(rel_or_abs)
    full = (p if p.is_absolute() else root / p).resolve()
    if root not in full.parents and full != root:
        raise ValueError(f"path escapes inbox root: {rel_or_abs}")
    return full


def _is_user_dir(name: str) -> bool:
    return bool(_USER_DIR_RE.match(name))


def owner_user_id_from_path(path: Path) -> int | None:
    """The user_id owning `path`, inferred from its `u<id>` parent segment.

    Returns None for a path in the flat legacy tree (no `u<id>` segment) or
    outside the inbox root entirely. Used by the background transcription/
    vision sweeps, which walk every user's files but still want to build a
    per-user proper-noun dictionary rather than an arbitrary one.
    """
    try:
        rel = path.resolve().relative_to(inbox_root().resolve())
    except ValueError:
        return None
    if not rel.parts:
        return None
    m = _USER_DIR_RE.match(rel.parts[0])
    return int(m.group(1)) if m else None


def sidecar_path(file_path: Path) -> Path:
    """Sidecar lives next to the file with `.meta.json` appended (full
    suffix preserved so `foo.pdf` → `foo.pdf.meta.json`, not `foo.meta.json`)."""
    return file_path.with_suffix(file_path.suffix + ".meta.json")


def _is_sidecar(p: Path) -> bool:
    return p.name.endswith(".meta.json")


def _is_internal_artifact(p: Path) -> bool:
    """Whether `p` is bookkeeping of ours rather than a captured item.

    Two kinds so far: the `.meta.json` sidecar, and the `.transcribing.lock`
    claimed by `_acquire_transcription_lock`.

    The lock matters more than it looks. Every walker here filtered on
    `_is_sidecar` alone, so the moment transcription started writing a lock
    file next to the audio, that lock became a *pending inbox item* — it would
    be enriched, given a sidecar of its own, and listed to the user as a
    mysterious empty file. Usually it would vanish again within the minute,
    which is the worst shape for a bug: rare, self-healing, and impossible to
    reproduce on demand. After a crashed transcribe it would sit there for the
    full 15-minute stale window instead.

    Anything else written *beside* a captured file in future belongs here too.
    The rule is: if we created it, the user should never see it in their inbox.
    """
    return _is_sidecar(p) or p.name.endswith(_TRANSCRIPTION_LOCK_SUFFIX)


# ---------------------------------------------------------------------------
# Kind sniffer + preview extractor
# ---------------------------------------------------------------------------


def _sniff_ftyp_brand(head: bytes) -> str | None:
    """Classify an ISO-BMFF file (MP4/M4A/MOV) by its ftyp major brand.

    Layout: `[4-byte box length][ftyp][4-byte major brand][...]`. The length is
    variable, so this has to look at bytes 4:8 for the marker rather than
    matching a fixed prefix — the bug this replaces. Returns "audio", "video",
    or None if this isn't ISO-BMFF at all.
    """
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    return "audio" if head[8:12] in _FTYP_AUDIO_BRANDS else "video"


def sniff_kind(path: Path) -> str:
    """Return a short kind label: pdf | text | markdown | image | audio |
    video | html | docx | xlsx | zip | unknown."""
    suffix = path.suffix.lower()
    if suffix in _EXT_KIND:
        return _EXT_KIND[suffix]
    try:
        with path.open("rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return "unknown"
    # Checked before `_MAGIC` because it inspects an offset rather than a
    # prefix, and nothing in `_MAGIC` can match an ISO-BMFF header anyway.
    ftyp_kind = _sniff_ftyp_brand(head)
    if ftyp_kind:
        return ftyp_kind
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    # A UTF-8 BOM is three high bytes, which fails the printable test below and
    # used to make any BOM-prefixed text file "unknown". Editors on Windows emit
    # one routinely, and a saved web page is exactly the kind of file that
    # arrives having been through one.
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    # Heuristic: if it's all printable ASCII/UTF-8, call it text. Deliberately
    # still only the first 16 bytes — widening this to the whole sniff buffer
    # would reclassify files that start ASCII and turn binary later, which is a
    # behaviour change nothing here asked for.
    if head[:16] and all(b == 9 or b == 10 or b == 13 or 32 <= b < 127 for b in head[:16]):
        # A saved web page is text, but calling it `text` means its preview is
        # 500 bytes of doctype and <head> boilerplate. Tines posts these with a
        # bare-UUID filename and no extension, so there is no suffix to fall
        # back on — the marker has to be found in the content.
        return "html" if _looks_like_html(head) else "text"
    return "unknown"


def _looks_like_html(head: bytes) -> bool:
    """True if `head` opens an HTML document.

    Scans a window rather than matching a prefix: a real page may lead with a
    BOM, a comment, or whitespace before `<!doctype`/`<html>`.

    Two conditions, because the marker alone is not enough — a plaintext note
    discussing "<html> tags" contains it too, and misfiling prose as a web page
    would send its preview through a tag stripper that deletes the very text the
    note consists of. So the document must *also* open with markup. Every real
    HTML file does: doctype, comment, XML declaration, or the tag itself.
    """
    try:
        window = head[:_SNIFF_BYTES].decode("utf-8-sig", errors="replace").lstrip().lower()
    except Exception:  # noqa: BLE001
        return False
    if not window.startswith("<"):
        return False
    return "<!doctype html" in window or "<html" in window


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
        if kind == "html":
            return _preview_html(path)
        if kind == "image":
            return _preview_image(path)
        if kind in ("audio", "video"):
            return _preview_media(path)
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


class _TitleAndTextParser(HTMLParser):
    """Pulls a page's `<title>` and its visible body text.

    Deliberately stdlib: this image already hand-rolls an MP4 box walker rather
    than take a dependency for one field (see `_mp4_duration_seconds`), and a
    title plus stripped text is well inside what `html.parser` handles. It does
    not need to be a correct HTML5 parser — it needs to beat showing the user a
    doctype declaration.
    """

    # Text inside these is markup machinery, not content — a saved page's
    # inlined <script> is usually the largest thing in the file, so failing to
    # skip it would just replace doctype boilerplate with minified JS.
    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self._in_title = False
        self._skip_depth = 0
        self._chunks: list[str] = []
        self._length = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "title" and self.title is None:
            self._in_title = True
        # `head` is in _SKIP but `title` lives inside it, so the title flag is
        # checked independently of the skip depth in handle_data.
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = ((self.title or "") + data)[:300]
            return
        if self._skip_depth or self._length >= PREVIEW_CHARS * 4:
            return
        text = data.strip()
        if text:
            self._chunks.append(text)
            self._length += len(text)

    def text(self) -> str:
        return " ".join(self._chunks)


def _preview_html(path: Path) -> tuple[str, dict[str, Any]]:
    """A saved web page's title and visible text, not its markup.

    The title is the payload here: it is the one field that reliably says what
    a captured page *is*, which is exactly what a push notification needs. The
    stripped text is a bonus and is often navigation chrome, so it follows the
    title rather than replacing it.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return "", {"preview_error": str(e)}

    parser = _TitleAndTextParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception as e:  # noqa: BLE001
        # Malformed markup must degrade to "we know it's a web page", never
        # fail the ingest that is trying to tell the user it arrived.
        logger.debug(f"[inbox] html parse incomplete for {path.name}: {e}")

    extras: dict[str, Any] = {"char_count": len(raw)}
    if parser.title:
        extras["title"] = " ".join(parser.title.split())
    return parser.text()[:PREVIEW_CHARS], extras


def _mp4_duration_seconds(path: Path) -> float | None:
    """Duration of an ISO-BMFF file from its `moov/mvhd` box, or None.

    Hand-rolled rather than pulling in mutagen/ffprobe: this walks top-level
    boxes for `moov`, then its children for `mvhd`, and reads the timescale and
    duration. That's ~30 lines against a stable 2001 container format, versus a
    new runtime dependency in the Docker image for one field.

    Returns None rather than raising on anything unexpected — a missing duration
    must degrade the preview, never fail the ingest.
    """
    import struct

    def _find_box(f, end_offset: int, wanted: bytes) -> tuple[int, int] | None:
        """Scan sibling boxes from the current position, return (start, end) of
        `wanted`'s payload."""
        while f.tell() + 8 <= end_offset:
            header_start = f.tell()
            header = f.read(8)
            if len(header) < 8:
                return None
            size = struct.unpack(">I", header[:4])[0]
            box_type = header[4:8]
            if size == 1:
                # 64-bit extended size lives in the next 8 bytes.
                ext = f.read(8)
                if len(ext) < 8:
                    return None
                size = struct.unpack(">Q", ext)[0]
                payload_start = header_start + 16
            elif size == 0:
                # "extends to end of file"
                size = end_offset - header_start
                payload_start = header_start + 8
            else:
                payload_start = header_start + 8
            if size < 8:
                return None  # malformed; refuse to loop forever
            box_end = header_start + size
            if box_type == wanted:
                return payload_start, box_end
            if box_end <= header_start:
                return None
            f.seek(box_end)
        return None

    try:
        file_size = path.stat().st_size
        with path.open("rb") as f:
            moov = _find_box(f, file_size, b"moov")
            if not moov:
                return None
            moov_start, moov_end = moov
            f.seek(moov_start)
            mvhd = _find_box(f, moov_end, b"mvhd")
            if not mvhd:
                return None
            f.seek(mvhd[0])
            version_flags = f.read(4)
            if len(version_flags) < 4:
                return None
            version = version_flags[0]
            if version == 1:
                # 64-bit: created(8) modified(8) timescale(4) duration(8)
                blob = f.read(28)
                if len(blob) < 28:
                    return None
                timescale = struct.unpack(">I", blob[16:20])[0]
                duration = struct.unpack(">Q", blob[20:28])[0]
            else:
                # 32-bit: created(4) modified(4) timescale(4) duration(4)
                blob = f.read(16)
                if len(blob) < 16:
                    return None
                timescale = struct.unpack(">I", blob[8:12])[0]
                duration = struct.unpack(">I", blob[12:16])[0]
            if not timescale:
                return None
            return duration / timescale
    except (OSError, struct.error):
        return None


def _format_seconds(seconds: float) -> str:
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if minutes else f"{secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _preview_media(path: Path) -> tuple[str, dict[str, Any]]:
    """Duration for audio/video. Deliberately does NOT transcribe.

    Transcription is a separate, much larger piece of work (it needs a
    transcriber reachable from the server — see the voice-memo integration in
    `Projects/lios/Backlog`). What this gives is enough to make a notification
    say something true and useful — "voice note, 1m 47s" instead of "a file" —
    and it costs no new dependency.

    If the caller already has a transcript (e.g. Tines transcribed before
    posting), it should arrive as `metadata.note` on the ingest payload; that is
    surfaced separately and takes precedence in the rendered summary.
    """
    duration = _mp4_duration_seconds(path)
    if duration is None:
        return "", {"note": "duration unavailable (not an ISO-BMFF container?)"}
    return "", {
        "duration_seconds": round(duration, 1),
        "duration_human": _format_seconds(duration),
    }


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


def content_hash(data: bytes) -> str:
    """Stable identity for an ingested file's bytes."""
    import hashlib

    return hashlib.sha256(data).hexdigest()


def record_item(
    user_id: int,
    relative_path: str,
    *,
    sha256: str | None = None,
    session: Session | None = None,
) -> None:
    """Upsert the `InboxItem` ownership row for one item.

    Called at ingest time (attributing to the caller) and at every bucket
    transition (`move_to`) so the row tracks the file's *current* location,
    which is what keeps `find_by_hash` working across a terminal-bucket move.
    Also called by `adopt_legacy_files()` for pre-existing flat-tree files —
    that is this table's only "backfill", since the table itself is created
    empty (see `models.py`).

    Best-effort on the race: a duplicate insert (two concurrent ingests of
    the same freshly-created relative path — vanishingly unlikely in
    practice) falls back to an update rather than raising into the caller's
    write path, which has already safely written bytes to disk by the time
    this runs.
    """
    from app.integrations.inbox.models import InboxItem

    def _do(s: Session) -> None:
        row = (
            s.query(InboxItem)
            .filter_by(user_id=user_id, relative_path=relative_path)
            .first()
        )
        if row is not None:
            if sha256 and row.sha256 != sha256:
                row.sha256 = sha256
                s.commit()
            return
        try:
            s.add(InboxItem(user_id=user_id, relative_path=relative_path, sha256=sha256))
            s.commit()
        except IntegrityError:
            s.rollback()
            row = (
                s.query(InboxItem)
                .filter_by(user_id=user_id, relative_path=relative_path)
                .first()
            )
            if row is not None and sha256 and row.sha256 != sha256:
                row.sha256 = sha256
                s.commit()

    if session is not None:
        _do(session)
        return
    from app.db import get_db

    db = get_db()
    with db.session() as own_session:
        _do(own_session)


def adopt_legacy_files() -> int:
    """Adopt pre-existing flat-tree inbox items into `LEGACY_OWNER_USER_ID`'s
    per-user subtree, physically and in the `InboxItem` ledger.

    Idempotent by construction rather than by a tracked flag: a file that has
    already been adopted no longer exists at its flat-tree location (it was
    moved, not copied), so a repeat call over the same file finds nothing to
    do. That's also why this runs lazily on every read/enrichment entry point
    (`list_pending`, `enrich_pending`, `find_by_hash`) instead of as a
    one-shot startup migration — files can land in the flat tree only up
    until this deploy ships (the ingest route now always writes into a
    user's subtree), but any that were already sitting there need a place to
    go the first time anything looks at the inbox after the deploy.
    """
    root = inbox_root()
    if not root.is_dir():
        return 0
    adopted = 0
    for bucket in sorted(PENDING_BUCKETS | TERMINAL_BUCKETS):
        flat_dir = root / bucket
        if not flat_dir.is_dir():
            continue
        for p in list(flat_dir.iterdir()):
            if not p.is_file() or _is_internal_artifact(p):
                continue
            dest_dir = user_root(LEGACY_OWNER_USER_ID) / bucket
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / p.name
            if dest.exists():
                # Name collision with something already adopted or ingested
                # straight into user 1's tree — suffix rather than clobber.
                stem, suffix = p.stem, p.suffix
                i = 1
                while (dest_dir / f"{stem}-legacy-{i}{suffix}").exists():
                    i += 1
                dest = dest_dir / f"{stem}-legacy-{i}{suffix}"
            meta = read_sidecar(p)
            sc = sidecar_path(p)
            shutil.move(str(p), str(dest))
            if sc.exists():
                shutil.move(str(sc), str(sidecar_path(dest)))
            record_item(
                LEGACY_OWNER_USER_ID,
                f"{bucket}/{dest.name}",
                sha256=meta.get("sha256"),
            )
            adopted += 1
    if adopted:
        logger.info(
            f"[inbox] adopted {adopted} legacy flat-tree file(s) into "
            f"user {LEGACY_OWNER_USER_ID}'s inbox"
        )
    return adopted


def find_by_hash(digest: str, user_id: int) -> Path | None:
    """Locate an already-ingested file owned by `user_id` with this content
    hash, if any.

    **This is the authoritative dedup point, and it has to live on the server.**
    There are two independent producers — a phone posting via the Tines tunnel
    when out, and the Mac watcher when it's running — and neither can see what
    the other sent. The same recording reaching both paths would otherwise be
    transcribed twice and billed twice. A client-side ledger can save a pointless
    *upload*, but only the server can prevent a duplicate *charge*.

    Scoped to `user_id` (F6): dedup must never let one user's upload match
    against — and thus reveal the existence and path of — another user's
    file. Rooted at `user_root(user_id)` rather than the shared inbox root,
    so the directory walk itself is the enforcement, not an extra filter.

    Searches terminal buckets too: a memo that was already triaged into the
    vault and archived must not come back as new when the Mac syncs it a
    week later.

    Linear over sidecars rather than an index, same as before F6 — at this
    scale (hundreds of files per user) reading small JSON files is a few
    milliseconds. `InboxItem` (see `models.py`) is a separate ownership
    ledger, not consulted here; it exists for ingest-time attribution and
    the legacy-adoption audit trail, not as a dedup index.
    """
    if user_id == LEGACY_OWNER_USER_ID:
        adopt_legacy_files()

    root = user_root(user_id)
    if not root.is_dir():
        return None
    for bucket in sorted(PENDING_BUCKETS | TERMINAL_BUCKETS):
        directory = root / bucket
        if not directory.is_dir():
            continue
        for candidate in directory.iterdir():
            if not candidate.is_file() or _is_sidecar(candidate):
                continue
            if read_sidecar(candidate).get("sha256") == digest:
                return candidate
    return None


def _transcription_lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + _TRANSCRIPTION_LOCK_SUFFIX)


def _acquire_transcription_lock(path: Path) -> bool:
    """Claim exclusive rights to transcribe `path` right now.

    **Why this exists.** The ingest route (`POST /api/inbox/ingest`) now fires
    a background transcription the moment a file lands (Task C of the Tines
    retirement), and the `*/5 * * * *` cron (`transcribe_pending`) can wake up
    and reach the very same freshly-arrived file before either has written
    `transcribed_at` — both would then pay for the same transcription.
    "Read `transcribed_at`, then act" is not atomic across two separate call
    paths; `open(..., O_CREAT | O_EXCL)` is, even across two OS processes
    sharing the same inbox volume, because the kernel guarantees exactly one
    caller sees the create succeed.

    Returns True if the lock was claimed (by this call, or by reclaiming an
    orphaned one — see `_TRANSCRIPTION_LOCK_STALE_SECONDS`), False if someone
    else currently holds it.
    """
    lock_path = _transcription_lock_path(path)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = datetime.now(timezone.utc).timestamp() - lock_path.stat().st_mtime
        except OSError:
            # Lock vanished between the failed create and this stat (the
            # holder just finished) — the caller will simply retry next sweep.
            return False
        if age < _TRANSCRIPTION_LOCK_STALE_SECONDS:
            return False
        # Orphaned by a process that died mid-transcribe (container restart,
        # OOM). Reclaim rather than wedge this file behind a lock nobody will
        # ever clear.
        logger.warning(f"[inbox] stale transcription lock on {path.name}, reclaiming")
        try:
            lock_path.unlink()
        except OSError:
            return False
        return _acquire_transcription_lock(path)


def _release_transcription_lock(path: Path) -> None:
    try:
        _transcription_lock_path(path).unlink()
    except OSError:
        pass


# Emails larger than this do not carry the original file. Two reasons, and the
# second is the real one: SMTP2GO's message cap, and the fact that a capture's
# email is a *convenience copy* — the authoritative file is already in the inbox
# and referenced by path in every one of these messages. Silently failing to
# send a 40 MB attachment would cost the notification entirely; dropping the
# attachment and saying so costs nothing that matters.
EMAIL_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024


def _capture_email_html(heading: str, intro: str, section_label: str, content: str,
                        footer: str | None = None) -> str:
    """The one HTML shell both capture emails use.

    Reproduces the retired Tines `Done` template (its markup, its #00D9A1 rule)
    minus the "Automated by Tines" footer, which would be false the moment this
    shipped from comar's own pipeline.

    Shared rather than copied because there are now two senders — the transcript
    email and the document email — and a copied template is a fork with a
    guaranteed drift: nothing fails when two mail bodies disagree about their own
    branding, so nothing tells you they have. Same reasoning as `summarise()`
    being the single renderer behind a push and an `inbox_pending` row.
    """
    import html as _html

    tail = (
        f'<p style="margin:24px 0 0 0;color:#6a6a6a;font-size:13px;">{_html.escape(footer)}</p>'
        if footer else ""
    )
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; background-color: #f5f5f5;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f5f5f5;">
        <tr>
            <td align="center" style="padding: 40px 20px;">
                <table width="600" cellpadding="0" cellspacing="0" style="background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                    <tr>
                        <td style="padding: 32px 40px; border-bottom: 3px solid #00D9A1;">
                            <h1 style="margin: 0; color: #1a1a1a; font-size: 24px; font-weight: 600;">{_html.escape(heading)}</h1>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 32px 40px;">
                            <p style="margin: 0 0 24px 0; color: #4a4a4a; font-size: 16px; line-height: 1.5;">{_html.escape(intro)}</p>
                            <div style="background-color: #f8f9fa; border-left: 4px solid #00D9A1; padding: 20px; border-radius: 4px;">
                                <h2 style="margin: 0 0 12px 0; color: #1a1a1a; font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">{_html.escape(section_label)}</h2>
                                <p style="margin: 0; color: #4a4a4a; font-size: 14px; line-height: 1.6; white-space: pre-wrap; font-family: 'Courier New', monospace;">{_html.escape(content)}</p>
                            </div>{tail}
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""


def _original_file_attachment(path: Path, meta: dict[str, Any]):
    """The captured file itself, or None if it is too large or unreadable.

    Returning None rather than raising is the point: see
    `EMAIL_ATTACHMENT_MAX_BYTES`. The caller still sends the mail.
    """
    from app.integrations.notifications.facade import EmailAttachment

    try:
        if path.stat().st_size > EMAIL_ATTACHMENT_MAX_BYTES:
            return None
        content = path.read_bytes()
    except OSError:
        return None
    name = meta.get("original_filename") or path.name
    return EmailAttachment(
        filename=name,
        content_type=meta.get("content_type") or "application/octet-stream",
        content=content,
    )


def _notify_document_email(meta: dict[str, Any], path: Path) -> None:
    """Email a captured document — the `Send To Comar` half of the pipeline.

    **Why this exists.** Audio always had a second beat: a transcript arrives
    minutes later and is worth reading. A shared PDF had only the first — a
    lock-screen line that scrolls away — so the extracted text comar had already
    produced was thrown away, and the file was rediscovered by accident weeks
    later. The quotation captured on 2026-08-29 is the case in point: comar OCR'd
    it at ingest, put the vendor, quote number and total in a push, and then kept
    none of it anywhere a person would look.

    Fires once per document, at the point its text is final: inline at ingest for
    PDFs/text (enrichment is synchronous), and after the `*/15` description sweep
    for images. Audio is excluded — it has `_notify_transcript_success_email`, and
    two emails per memo is a worse outcome than none.

    Best-effort and swallowed, for the same reason as its siblings: the file is on
    disk before this runs, and a mail failure must never turn a good capture into
    a retried one.
    """
    owner = owner_user_id_from_path(path)
    if owner is None:
        logger.debug(f"[inbox] no owning user for document email ({path.name})")
        return

    content = (meta.get("preview") or meta.get("description") or "").strip()
    if not content:
        # Nothing to say beyond what the push already said. An email whose body
        # is a filename is worse than no email — it trains you to ignore them.
        logger.debug(f"[inbox] no extracted text for document email ({path.name})")
        return

    name = meta.get("original_filename") or path.name
    title = meta.get("title")
    subject = f"Captured: {title or name}"

    kind = meta.get("kind") or "file"
    section = "Description" if kind == "image" else "Extracted text"
    summary_line = summarise(meta, size_bytes=_size_or_none(path))
    footer = f"{name} — {summary_line}\nStill on disk at {path}"

    attachment = _original_file_attachment(path, meta)
    if attachment is None:
        footer += "\n(Original not attached — too large.)"

    body_html = _capture_email_html(
        "Document Captured", "A file was captured to your inbox.", section, content,
        footer=footer,
    )
    body_text = f"A file was captured to your inbox.\n\n{content}\n\n{footer}\n"

    try:
        from app.plugin.capabilities import get_capability

        get_capability("notify.email").send_email(
            owner, subject, body_html, body_text,
            attachments=[attachment] if attachment else None,
        )
    except Exception:  # noqa: BLE001
        logger.debug(f"[inbox] document email failed for {path.name}", exc_info=True)


def _size_or_none(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _notify_transcript_success_email(meta: dict[str, Any], path: Path, transcript: str) -> None:
    """Email the finished transcript — best-effort, the `Done` half of the
    retired Tines story (see `../tines-reference.json`).

    Reproduces its HTML template verbatim except the footer: the original
    read "Automated by Tines", which would be false the instant this ships
    from comar's own pipeline instead, so it is dropped rather than
    relabelled.

    Fired from the same success branch as the push in `transcribe_file`,
    right after the transcript is durably written to the sidecar — email is
    a convenience layered on an already-safe capture, never a condition for
    one, so every failure here is caught and swallowed. A dropped/misconfigured
    send must never turn a successful transcription into a retried one.
    """
    owner = owner_user_id_from_path(path)
    if owner is None:
        logger.debug(f"[inbox] no owning user for transcript email ({path.name})")
        return

    # Deliberately name-free — see `TranscriptResult.title`'s docstring — an
    # email subject line is exactly the kind of place a name must not land.
    title = meta.get("title")
    subject = f"Transcript complete: {title}" if title else "Transcript complete"

    body_html = _capture_email_html(
        "Transcript Complete", "Your transcript has been processed.",
        "Transcript", transcript,
    )
    body_text = f"Your transcript has been processed.\n\n{transcript}\n"

    try:
        from app.integrations.notifications.facade import EmailAttachment
        from app.plugin.capabilities import get_capability

        attachment = EmailAttachment(
            filename="transcript.txt", content_type="text/plain",
            content=transcript.encode("utf-8"),
        )
        get_capability("notify.email").send_email(
            owner, subject, body_html, body_text, attachments=[attachment],
        )
    except Exception:  # noqa: BLE001
        logger.debug(f"[inbox] transcript email failed for {path.name}", exc_info=True)


def _notify_transcript_failure_email(meta: dict[str, Any], path: Path) -> None:
    """Email the give-up notice — the `Not Done` half of the retired Tines
    story. Fired from the exact give-up branch that already pushes
    `notify.push` (see `transcribe_file`), and just as best-effort/swallowed
    as the success email above.

    ⚠️ **It has to name the recording.** Tines' original wording was the whole
    body — "No usable transcript was produced. Please try again." — which
    tells you that *something* failed and nothing about *what*. Capture two
    memos in a morning and it is unactionable: you cannot tell which one died,
    and the audio is sitting intact in the inbox where nobody thinks to look.
    "Try again" is also the wrong instruction on its own, because it implies
    the recording is gone; it is not, and re-recording a ten-minute thought
    you already had is the expensive way to recover from a retryable error.

    So this carries what the recipient needs in order to act: the name they
    gave it, when they captured it, how long it is, and where the file still
    is. `meta` is threaded in for exactly that — the success email already
    took it, and the failure email taking less than the success email was
    backwards.
    """
    owner = owner_user_id_from_path(path)
    if owner is None:
        logger.debug(f"[inbox] no owning user for failure email ({path.name})")
        return

    name = meta.get("original_filename") or path.name
    duration = (meta.get("preview_meta") or {}).get("duration_human")
    captured = meta.get("ingested_at")

    subject = f"Transcription failed: {name}"

    facts = [("Recording", name)]
    if duration:
        facts.append(("Length", duration))
    if captured:
        facts.append(("Captured", str(captured).replace("T", " ")[:19]))
    facts.append(("Still on disk at", str(path)))

    body_text = (
        "No usable transcript was produced after three attempts.\n\n"
        + "\n".join(f"{k}: {v}" for k, v in facts)
        + "\n\nThe audio itself is safe and has not been deleted — there is no "
          "need to re-record. It can be retried from the inbox.\n"
    )

    import html as _html

    rows = "".join(
        f'<tr><td style="padding:4px 16px 4px 0;color:#6a6a6a;font-size:13px;">{_html.escape(k)}</td>'
        f'<td style="padding:4px 0;color:#1a1a1a;font-size:13px;">{_html.escape(str(v))}</td></tr>'
        for k, v in facts
    )
    body_html = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif;">'
        "<p>No usable transcript was produced after three attempts.</p>"
        f"<table cellpadding='0' cellspacing='0'>{rows}</table>"
        "<p style='color:#4a4a4a;'>The audio itself is safe and has not been deleted — "
        "there is no need to re-record. It can be retried from the inbox.</p>"
        "</div>"
    )

    try:
        from app.plugin.capabilities import get_capability

        get_capability("notify.email").send_email(owner, subject, body_html, body_text)
    except Exception:  # noqa: BLE001
        logger.debug(f"[inbox] failure email failed for {path.name}", exc_info=True)


def transcribe_file(path: Path, *, prefer: str = "openai") -> str:
    """Transcribe exactly one file if — and only if — it still needs it.

    This is the single-file unit of work behind two callers: `transcribe_pending`'s
    sweep (below) and the ingest route's immediate background transcription
    (`POST /api/inbox/ingest`, Task C of the Tines retirement — see
    `transcribe_file_task`). One implementation, two callers, on purpose:
    duplicating the retry/idempotency/notify/email logic between a cron path
    and a request path is exactly the kind of fork that went stale silently
    once already in this codebase (the commute solver, forked between
    `hardware/homeassistant` and comar — see `Code/CLAUDE.md`).

    Returns one of:
      - `"unavailable"` — transcription isn't configured and `prefer` isn't
        `"embedded"`
      - `"not_audio"` — not an audio/video file, nothing to do
      - `"skipped"` — already transcribed (`transcribed_at` set)
      - `"in_progress"` — another caller is transcribing this file right now
        (see `_acquire_transcription_lock`)
      - `"transcribed"` / `"retrying"` / `"failed"` — the outcome of an
        attempt; see the retry-migration note this replaces in
        `transcribe_pending`'s docstring for what each means.
    """
    from app.plugin.capabilities import get_capability

    transcription = get_capability("transcription.audio")
    if not transcription.available() and prefer != "embedded":
        return "unavailable"

    meta = read_sidecar(path)
    kind = meta.get("kind") or sniff_kind(path)
    if kind not in ("audio", "video"):
        return "not_audio"
    if meta.get("transcribed_at"):
        return "skipped"

    if not _acquire_transcription_lock(path):
        return "in_progress"

    try:
        result = transcription.transcribe(
            path, prefer=prefer,
            vault_root=_vault_root_for_dictionary(owner_user_id_from_path(path)),
            # The ledger's cost-per-minute denominator; the sidecar already
            # knows it from enrichment, the transcriber never did.
            duration_s=(meta.get("preview_meta") or {}).get("duration_seconds"),
        )

        meta["transcript_source"] = result.source
        if result.error:
            meta["transcript_error"] = result.error[:300]

        # `transcribed_at` gates the idempotency check above, so it must only
        # be set on a TERMINAL outcome — one where retrying could not help:
        #
        #   - success (`result.text` set)
        #   - genuine silence: empty text, no error at all. This is not a
        #     failure, it's what a pocket recording looks like, and it is
        #     recorded here (not just implied) rather than in the `else`
        #     branch below, precisely so it can never be confused with a
        #     transient failure that happens to also carry no error text.
        #   - a permanent error (`result.transient` is False)
        #   - a transient error that has already been retried
        #     `MAX_TRANSCRIPTION_ATTEMPTS` times — at that point retrying is
        #     no longer distinguishable from failing forever, so it converts
        #     to a give-up.
        #
        # A transient error under the cap must NOT stamp `transcribed_at`:
        # doing so was the original bug (Tines retried 3x on a 5xx; comar
        # stamped immediately and buried the memo on the very first blip).
        attempts = int(meta.get("transcript_attempts") or 0) + 1
        transient_retry = bool(result.transient) and not result.text and attempts < MAX_TRANSCRIPTION_ATTEMPTS

        if transient_retry:
            meta["transcript_attempts"] = attempts
            write_sidecar(path, meta)
            return "retrying"

        meta["transcribed_at"] = datetime.now(timezone.utc).isoformat()
        meta.pop("transcript_attempts", None)
        if result.text:
            # Land it in `note`, which is what `summarise()` leads with and what
            # `inbox_pending` surfaces — so the transcript is the thing a human
            # (or a triage pass) actually sees, not a buried field.
            meta["note"] = result.text
            if result.title:
                meta["title"] = result.title
            if result.speakers is not None:
                # Kept because it is the one cheap signal for "meeting" vs
                # "note to self" — the retired Tines story used exactly this to
                # choose between `meeting-note.md` and `voice-note.md`, and a
                # triage pass wants the same distinction. It was being produced
                # by the transcriber, carried through `TranscriptResult`, and
                # then dropped here, which is the easiest kind of gap to keep:
                # nothing fails, the field is simply always absent.
                meta["speakers"] = result.speakers
            outcome = "transcribed"
        else:
            outcome = "failed"

        write_sidecar(path, meta)

        if result.text:
            _notify_enriched(
                meta, path, "lios: voice note transcribed", title=meta.get("title"),
            )
            _notify_transcript_success_email(meta, path, result.text)
        elif result.error:
            # A genuine failure just went terminal — either it was permanent
            # from the first attempt, or it was transient and just exhausted
            # its retry budget. Either way this is the exact point Tines used
            # to reach and email the user from ("Transcription Failed - Try
            # again."), so it gets the same higher-than-success push severity
            # plus the matching failure email; genuine silence (no error at
            # all) is not a failure and is excluded here — see the
            # `transcribed_at` comment above.
            _notify_enriched(
                meta, path, "lios: voice note transcription failed — try again",
                severity="warning",
            )
            _notify_transcript_failure_email(meta, path)

        return outcome
    finally:
        _release_transcription_lock(path)


def transcribe_pending(prefer: str = "openai", limit: int = 20) -> dict[str, int]:
    """Transcribe pending audio that hasn't been transcribed yet.

    Separate from `enrich_pending` on purpose. Enrichment is cheap and local
    (magic bytes, a header read) and runs inline in the ingest request;
    transcription is slow and *costs money per call*, so it belongs on a
    background cron where a long memo can take minutes without timing out a
    client, and where a failure can be recorded rather than retried in a loop.

    Idempotency is the whole game here, because retrying is billable. A file is
    skipped once `transcribed_at` is set — including when the result was empty
    (`transcript_source: "none"`), since a silent recording would otherwise be
    re-sent on every sweep forever. The only way to force a retry is to clear
    that field.

    `limit` bounds spend per sweep: a backfill of hundreds of files walks through
    over successive runs instead of issuing hundreds of concurrent paid requests.

    **Retry, replacing the retired Tines story.** Tines used to sit in front of
    this call and retry 3x on a 5xx before giving up and emailing the user
    "Transcription Failed - Try again." Moving transcription server-side
    (2026-07-31) dropped that behaviour rather than replacing it: a transient
    Gemini 503 or network blip used to stamp `transcribed_at` immediately
    (before the outcome was even inspected), which is indistinguishable from a
    permanent failure to the idempotency check above `transcribed_at` gates
    on — so a blip buried the memo forever with no retry and no notice. Now a
    `TranscriptResult.transient` result leaves `transcribed_at` unset (so the
    next sweep retries it) and only increments `transcript_attempts`; after
    `MAX_TRANSCRIPTION_ATTEMPTS` tries it gives up, stamps, and pushes +
    emails a failure notification — matching Tines' `retries: 3` and its
    "give up and tell the user" ending, just moved onto this cron's
    5-minute cadence instead of Tines' own retry loop.

    **Since Task C (immediate background transcription on ingest):** this
    loop still walks every pending file and still owns the `considered`/
    `skipped` counters (a file the background task already claimed reads
    back as `transcribed_at` set, same as any other already-done file), but
    the actual attempt — including the retry/notify/email logic — is
    `transcribe_file()`'s, shared with that background task. This sweep is
    now the *sweeper/retry net*: it catches anything the background task
    missed (transcription wasn't configured yet, the app restarted between
    ingest and the background task running, a file arrived through some
    other producer that doesn't call the ingest route) rather than being the
    only path a voice note is ever transcribed through.
    """
    from app.plugin.capabilities import get_capability

    transcription = get_capability("transcription.audio")

    counts = {
        "considered": 0, "transcribed": 0, "skipped": 0, "failed": 0,
        # A transient failure that will be retried on the next sweep — kept
        # separate from "failed" so a dashboard/log line doesn't read a
        # network blip as the same kind of outcome as a give-up.
        "retrying": 0,
    }

    # Nothing configured → do nothing, quietly. Without this an unconfigured
    # deployment would log a failure for every pending audio file every sweep.
    if not transcription.available() and prefer != "embedded":
        return counts

    for path in iter_all_pending_files():
        # A transient retry still spends a paid call, so it counts toward the
        # per-sweep spend cap the same as a transcribed/give-up outcome.
        if counts["transcribed"] + counts["failed"] + counts["retrying"] >= limit:
            break

        meta = read_sidecar(path)
        kind = meta.get("kind") or sniff_kind(path)
        if kind not in ("audio", "video"):
            continue

        counts["considered"] += 1
        if meta.get("transcribed_at"):
            counts["skipped"] += 1
            continue

        outcome = transcribe_file(path, prefer=prefer)

        if outcome == "transcribed":
            counts["transcribed"] += 1
        elif outcome == "retrying":
            counts["retrying"] += 1
        elif outcome == "failed":
            counts["failed"] += 1
        # "in_progress" (the background task from ingest got there first —
        # not a failure, just means this sweep has nothing to do here right
        # now) and "unavailable"/"not_audio"/"skipped" (already excluded by
        # the checks above, kept here only so this stays correct if that
        # ever changes) fall through as no-ops.

    if counts["transcribed"] or counts["failed"] or counts["retrying"]:
        logger.info(f"[inbox] transcription sweep: {counts}")
    return counts


async def transcribe_file_task(path: Path, *, prefer: str = "openai") -> None:
    """Background-task entry point for the ingest route (Task C of the Tines
    retirement, `POST /api/inbox/ingest`): kick off transcription for THIS
    file the moment it lands, rather than waiting for the next `*/5 * * * *`
    cron tick. Mirrors `transcribe_pending_task`'s to_thread/log-and-swallow
    shape — a `BackgroundTasks` callable's exception is otherwise dropped
    silently by Starlette once the response has already gone out, which
    would make a transcription bug invisible.

    Races the cron sweep on the very same file by design — see
    `_acquire_transcription_lock` for the guard that stops both from paying
    for the same transcription.
    """
    import asyncio

    try:
        await asyncio.to_thread(transcribe_file, path, prefer=prefer)
    except Exception:
        logger.exception(f"[inbox] background transcription failed for {path.name}")


def _vault_root_for_dictionary(user_id: int | None) -> Path | None:
    """Vault root used to build the proper-noun prompt, or None if unresolvable.

    `user_id` comes from `owner_user_id_from_path()` — the transcription
    sweep walks every user's files, so the dictionary must be built from the
    owning user's own vault (their People notes' aliases), not whichever
    user happens to be bound to the background task's context (none, in
    practice — this runs off a cron, not a request).

    Best-effort: a missing vault costs dictionary quality, not the transcript.
    """
    if user_id is None:
        logger.debug("[inbox] no owning user for dictionary lookup; transcribing without one")
        return None
    try:
        from app.services import vault_paths

        return vault_paths.resolve(".", user_id_override=user_id)
    except Exception:  # noqa: BLE001
        logger.debug("[inbox] vault root unresolved; transcribing without a dictionary")
        return None


def _notify_enriched(
    meta: dict[str, Any], path: Path, headline: str, severity: str = "recovery",
    *, title: str | None = None,
) -> None:
    """Push a finished description of an ingested item, best-effort.

    The ingest-time confirmation can only describe what is knowable from the
    bytes — "voice note, 1m 47s", "image, 1206×2622". This is the follow-up that
    actually carries the *content*, once the sweep that costs money has run, and
    it is the point of those sweeps having run at all.

    Shared by the transcription and vision paths: same payload, same headline
    logic, only `severity` differs. `summarise()` is the single renderer, so a
    push and an `inbox_pending` row can never disagree about what a file is.

    `severity` defaults to "recovery" — informational, not a problem, the
    right priority for every existing call site (a successful enrichment).
    The transcription give-up path (`transcribe_pending`) passes "warning"
    instead: that push is the point of the retire-Tines migration, not an FYI.

    `title` (added alongside `TranscriptResult.title`): when the caller has a
    Gemini-parsed document title for this item, it replaces the generic
    `headline` on the notification the phone actually shows — "call the
    plumber about the utility room" on a lock screen is worth far more than
    the same "lios: voice note transcribed" every capture used to show.
    Deliberately **name-free** (see `TranscriptResult.title`'s docstring for
    why the field itself is built that way): a lock screen is visible to
    whoever glances at the phone, not just its owner. `None` — the default,
    and every non-transcription call site (vision, the ingest confirmation)
    — keeps today's generic headline unchanged.

    ⚠️ **The recipient is derived here, from the file's own path — not passed
    in by the caller.** Until 2026-08-30 this called `send()` with no
    `user_id`, which `client._resolve_targets` reads as *household-wide* and
    resolves to `household_targets` — one entry, Alex's phone. Every capture
    notification therefore went to one phone regardless of who captured it:
    Sam's voice notes notified Alex, and the Gemini title of her memo — a
    field built name-free precisely because a lock screen is semi-public —
    landed on somebody else's lock screen. The owner was never unknown;
    `_notify_transcript_success_email`, two frames away, was already calling
    `owner_user_id_from_path(path)` to route the matching email correctly.
    Push and email simply disagreed, and only one of them was right.

    🔑 **Resolved here rather than added as a parameter, deliberately.** A
    `user_id` argument would have to be supplied correctly by all four call
    sites and by every one added later, and the failure mode of forgetting is
    silent — it is *exactly* how this bug existed in the first place, since
    `send()`'s `user_id` is already an optional parameter whose omission means
    "household". Deriving it from `path` — which every call site must pass
    anyway, and which is the same source of truth the email path uses — makes
    correct routing the thing that happens by default and misrouting the thing
    you would have to work at.

    A `None` owner (a path in the flat legacy tree, or outside the inbox root)
    still falls back to household-wide, which is the pre-F6 behaviour and the
    only sensible answer when the file genuinely has no owner.
    """
    try:
        from app.plugin.capabilities import get_capability

        get_capability("notify.push").send(
            f"lios: {title}" if title else headline,
            summarise(meta, size_bytes=path.stat().st_size),
            severity,
            user_id=owner_user_id_from_path(path),
        )
    except Exception:  # noqa: BLE001
        logger.debug(f"[inbox] {headline!r} notification unavailable", exc_info=True)


def notify_ingested(meta: dict[str, Any], path: Path) -> bool:
    """Announce that something landed in the inbox, if configured to.

    **Why this is off by default.** Until now the ingest confirmation was sent by
    the *caller*: the route returns a rendered `summary` and the Tines story
    pushes it. That works, and it is the only reason Tines is still in the capture
    path at all — transcription moved server-side in 2026-07-31, and the tunnel is
    replaceable by the tailnet address the phone already uses for Health Auto
    Export. A producer that posts here directly (an iOS Shortcut) can move bytes
    but cannot describe what the server made of them, so it needs the server to
    speak.

    Both at once would announce every capture twice, so this is config-gated
    rather than unconditional: enable `inbox_confirm_push` in the same change that
    stops the relay pushing. Returns whether a push was attempted, so a caller (or
    a test) can tell "disabled" from "failed".
    """
    from app.plugin.config_store import plugin_config

    if not plugin_config("inbox").inbox_confirm_push:
        return False

    kind = meta.get("kind") or "file"
    label = {"audio": "voice note", "image": "image", "html": "web page"}.get(kind, kind)
    _notify_enriched(meta, path, f"lios: {label} captured")
    return True


def email_ingested_document(meta: dict[str, Any], path: Path) -> bool:
    """Send the document email for a just-ingested capture, if it is due one.

    Called from the ingest request, right after `confirm_ingest`. Which kinds
    are handled *here* versus later is decided by when their text becomes final,
    not by anything about the file:

      - `audio`  — never here. Its text arrives minutes later from a paid sweep;
                   `_notify_transcript_success_email` owns it.
      - `image`  — never here. Same reason: the description comes from the `*/15`
                   vision sweep, so at ingest there is nothing to say.
      - anything else (pdf, text, html) — here. Enrichment is synchronous and
        inline, so the extracted text already exists by the time this runs, and
        deferring it would add latency for no gain.

    ⚠️ Deliberately **not** gated on `inbox_confirm_push`. That flag exists to
    stop a *push* being sent twice while a relay was also sending one; it says
    nothing about email, and reusing it here would silently couple two unrelated
    decisions — turning the push off would take the durable copy with it, which
    is the opposite of what anyone reaching for that flag intends.

    Returns whether an email was attempted, so a caller or test can tell
    "not due one" from "failed".
    """
    kind = meta.get("kind") or "file"
    if kind in ("audio", "image"):
        return False
    _notify_document_email(meta, path)
    return True


async def transcribe_pending_task() -> None:
    """Cron entry point (see `manifest.py::background_tasks`)."""
    import asyncio

    try:
        await asyncio.to_thread(transcribe_pending)
    except Exception:
        logger.exception("[inbox] transcription sweep failed")


def describe_pending(limit: int = 20) -> dict[str, int]:
    """Describe and OCR pending images that haven't been described yet.

    The image sibling of `transcribe_pending`, and separate from
    `enrich_pending` for the same reason: enrichment is cheap and local (magic
    bytes, image dimensions) and runs inline in the ingest request, while this
    costs money per call and depends on a third party being reachable.

    Idempotency matters because retrying is billable. A file is skipped once
    `described_at` is set — *including* when the result was empty, since an
    image the model declined or couldn't read would otherwise be re-sent every
    sweep forever. Clearing that field is the only way to force a retry.

    `limit` bounds spend per sweep, so a folder of holiday photos walks through
    over successive runs instead of issuing a hundred paid calls at once.
    """
    from app.plugin.capabilities import get_capability

    vision = get_capability("vision.image")

    counts = {"considered": 0, "described": 0, "skipped": 0, "failed": 0}

    # Nothing configured → do nothing, quietly. Without this an unconfigured
    # deployment logs a failure for every pending image on every sweep.
    if not vision.available():
        return counts

    for path in iter_all_pending_files():
        if counts["described"] + counts["failed"] >= limit:
            break

        meta = read_sidecar(path)
        kind = meta.get("kind") or sniff_kind(path)
        if kind != "image":
            continue

        counts["considered"] += 1
        if meta.get("described_at"):
            counts["skipped"] += 1
            continue

        result = vision.describe(path)

        meta["described_at"] = datetime.now(timezone.utc).isoformat()
        if result.model:
            meta["vision_model"] = result.model
        if result.error:
            meta["vision_error"] = result.error[:300]
        if result.kind:
            meta["image_kind"] = result.kind

        if result.ok:
            # `note` is what `summarise()` leads with and what `inbox_pending`
            # surfaces, so the description is the thing a human (or a triage
            # pass) actually sees — mirroring where a transcript lands.
            meta["note"] = result.summary
            if result.text:
                # Kept separate from `note`: the verbatim transcription is what
                # makes a photographed letter searchable once it reaches the
                # corpus, but it's the wrong thing to show in a one-line
                # inbox listing.
                meta["image_text"] = result.text
            counts["described"] += 1
        else:
            counts["failed"] += 1

        write_sidecar(path, meta)

        # The image sibling of the transcript push. Without this an image's only
        # notification was the ingest-time one, which knows nothing but the
        # dimensions and a bare-UUID filename — so a screenshot arrived as
        # "image, 1206×2622 (BE10BE85-AA94-…)" and stayed that way.
        if result.ok:
            _notify_enriched(meta, path, "lios: image described")
            # Beat two for an image: the push carries the headline, this carries
            # the description itself somewhere it survives being scrolled past.
            _notify_document_email(meta, path)

    if counts["described"] or counts["failed"]:
        logger.info(f"[inbox] vision sweep: {counts}")
    return counts


async def describe_pending_task() -> None:
    """Cron entry point (see `manifest.py::background_tasks`)."""
    import asyncio

    try:
        await asyncio.to_thread(describe_pending)
    except Exception:
        logger.exception("[inbox] vision sweep failed")


def summarise(meta: dict[str, Any], *, size_bytes: int | None = None) -> str:
    """One human-readable line describing an ingested item.

    This exists because the ingest route's JSON response is the *only* material
    the Tines webhook flow has to build its ntfy confirmation from, and that
    response used to carry nothing but a generated filename and a byte count —
    so every confirmation read the same regardless of what had been captured.
    Rendering the line here rather than in Tines keeps it in version control and
    identical between the notification and `inbox_pending`.

    Priority order is deliberate: a caller-supplied `note` (e.g. a transcript
    Tines already produced) beats an extracted `preview`, which beats
    kind-specific metadata, which beats a bare size.
    """
    kind = meta.get("kind") or "file"
    extras = meta.get("preview_meta") or {}
    label = {
        "audio": "voice note", "video": "video", "image": "image",
        "html": "web page",
    }.get(kind, kind)

    bits: list[str] = [label]

    if kind in ("audio", "video") and extras.get("duration_human"):
        bits.append(extras["duration_human"])
    elif kind == "image" and extras.get("width") and extras.get("height"):
        bits.append(f"{extras['width']}×{extras['height']}")
    elif kind == "pdf" and extras.get("page_count"):
        pages = extras["page_count"]
        bits.append(f"{pages} page{'s' if pages != 1 else ''}")

    if size_bytes:
        bits.append(f"{size_bytes / 1024:.0f} KB")

    header = ", ".join(bits)

    # `note` first — if something upstream already knows what this file says,
    # that beats anything extracted here.
    body = (meta.get("note") or "").strip() or (meta.get("preview") or "").strip()

    # A page's own <title> outranks its extracted text, which is usually cookie
    # banners and nav links. This sits below `note` on purpose: if vision or a
    # triage pass has since written a real description, that still wins.
    if kind == "html" and not (meta.get("note") or "").strip() and extras.get("title"):
        title = extras["title"].strip()
        body = f"{title} — {body}" if body else title

    if body:
        # Collapse whitespace so a multi-line transcript stays a single
        # notification line, and cap it so a long PDF doesn't flood the push.
        flattened = " ".join(body.split())
        if len(flattened) > 240:
            flattened = flattened[:237].rstrip() + "…"
        return f"{header} — {flattened}"

    if meta.get("original_filename"):
        return f"{header} ({meta['original_filename']})"
    return header


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


def iter_pending_files(user_id: int) -> list[Path]:
    """All non-sidecar files in any of `user_id`'s pending buckets. Sorted
    oldest first so triage tackles backlog in arrival order.

    Scoped to one user's subtree (F6) — this is what every caller-facing
    read (`list_pending`, the inline-enrich pass in `handle_pending`) walks,
    so it must never reach into another user's files."""
    root = user_root(user_id)
    out: list[Path] = []
    for bucket in PENDING_BUCKETS:
        d = root / bucket
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and not _is_internal_artifact(p):
                out.append(p)
    out.sort(key=lambda p: p.name)  # timestamp-prefixed → chronological
    return out


def iter_all_pending_files() -> list[Path]:
    """All non-sidecar pending files across EVERY user's subtree.

    For the background maintenance sweeps only (`enrich_pending`,
    `transcribe_pending`, `describe_pending`) — these write sidecar
    metadata, never return content to a specific caller, so walking every
    user's tree is a maintenance operation, not a scoping violation. Legacy
    flat-tree files are deliberately excluded here: callers that care about
    them call `adopt_legacy_files()` first (as `enrich_pending` and
    `list_pending` do), after which they've moved into a `u<id>` subtree and
    this function picks them up on the very next call.
    """
    root = inbox_root()
    out: list[Path] = []
    if not root.is_dir():
        return out
    for entry in root.iterdir():
        if not entry.is_dir() or not _is_user_dir(entry.name):
            continue
        for bucket in PENDING_BUCKETS:
            d = entry / bucket
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_file() and not _is_internal_artifact(p):
                    out.append(p)
    out.sort(key=lambda p: p.name)
    return out


def count_pending(user_id: int) -> int:
    return len(iter_pending_files(user_id))


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
    `enriched_at`. Cheap to re-run.

    Runs `adopt_legacy_files()` first — this is the hourly cron entry point
    (see `manifest.py`), so it's the natural place for the lazy legacy-tree
    migration to happen even if nobody has called `list_pending` yet."""
    adopt_legacy_files()
    pending = iter_all_pending_files()
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


def list_pending(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """One dict per `user_id`'s pending file, sidecar merged in, suitable
    for the `inbox_pending` MCP tool.

    Adopts legacy flat-tree files into user 1's subtree first (a no-op for
    every other user, and a no-op once nothing remains in the flat tree) so
    Alex's pre-split backlog shows up under his own account without waiting
    for the hourly cron."""
    if user_id == LEGACY_OWNER_USER_ID:
        adopt_legacy_files()
    root = user_root(user_id).resolve()
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for p in iter_pending_files(user_id)[:limit]:
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
            # `note` was written to the sidecar at ingest but never returned
            # here, so a caller-supplied transcript or comment was stored and
            # then invisible to every consumer of this list.
            "note": meta.get("note"),
            "extra": meta.get("extra"),
            "enriched": bool(meta.get("enriched_at")),
            # Vision's own outputs. These were written to the sidecar by the
            # vision sweep and then never returned here — the *same* bug as
            # `note` above, one field along, and it cost a real lookup: a
            # photographed coffee-bag label had its varieties, altitude and
            # roast date transcribed into `image_text`, while every consumer of
            # this list saw only the one-line `summary` and had no way to reach
            # them. Capped like `preview`; `inbox_preview` returns the whole
            # thing.
            "image_kind": meta.get("image_kind"),
            "image_text": (meta.get("image_text") or "")[:PREVIEW_CHARS] or None,
            "image_text_truncated": len(meta.get("image_text") or "") > PREVIEW_CHARS,
            # `enriched` above is the INGEST pass; vision is a separate, later
            # sweep. Reporting one as the other is what made a failed
            # description indistinguishable from a successful one: an image
            # arrives `enriched: true, note: null` whether vision has not run
            # yet, has run and failed, or has run and been safety-blocked.
            # `described` says the sweep reached this file; `vision_error` says
            # what happened. Neither was previously visible to any caller.
            "described": bool(meta.get("described_at")),
            "vision_error": meta.get("vision_error"),
            "summary": summarise(meta, size_bytes=st.st_size),
        })
    return out


# ---------------------------------------------------------------------------
# State transitions (used by the routing tools)
# ---------------------------------------------------------------------------


def move_to(file_path: Path, terminal: str, user_id: int) -> Path:
    """Move file + sidecar into `user_id`'s /inbox/u<user_id>/<terminal>/.
    Terminal must be 'archive' or 'dismissed'.

    Callers must have already confirmed `file_path` is inside `user_id`'s
    own subtree (`safe_resolve` does this) — this function itself doesn't
    re-check, since by the time a caller has a concrete `Path` it has
    already gone through that gate."""
    if terminal not in TERMINAL_BUCKETS:
        raise ValueError(f"invalid terminal bucket: {terminal}")
    dest_dir = user_root(user_id) / terminal
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
    # No `InboxItem` update here: `find_by_hash` walks the filesystem (see its
    # docstring), and the sidecar — including `sha256` — moved with the file
    # above, so dedup keeps working across this bucket transition with no DB
    # write needed. `InboxItem` is the ingest-time/adoption-time ownership
    # ledger, not a location index that has to track every move.
    return dest
