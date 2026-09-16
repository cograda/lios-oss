"""Office-document format sniffing, shared by every ingestion path that has
to decide which parser a file needs (issue #140).

A file's real format is never trustworthy from its label — a filename
extension (`historical_corpus`/`inbox`) and a sender-declared mime_type
(`attachments`) are both untrusted metadata for exactly the same reason
`core/CLAUDE.md`'s Known Issues records for `sniff_kind`'s ftyp/HTML fixes
and the twin `mime_for` bugs in `vision`/`transcription`: nothing arriving at
these ingestion paths carries a filename or label that can be trusted. A
`.doc` file that is really OOXML (a zip containing `word/document.xml`) is
the same class of bug, just for Office documents rather than audio/video.

Lives in `app/services/` rather than inside any one integration package so
`historical_corpus`, `attachments` and `inbox` can all import it directly —
`app/integrations/*` packages may not import each other's internals except
via a declared `<pkg>.facade`, and both `historical_corpus` and `attachments`
already `depends_on=["corpus.ingest"→..., ...]` chains that `inbox` sits
downstream of (`inbox` itself `depends_on=["corpus.ingest", ...]`), so a
capability the other direction (corpus/attachments depending on inbox) would
close a cycle. This module has no integration identity at all, so that
question doesn't arise — it is infrastructure, like `app/services/text.py`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

_SNIFF_BYTES = 2048

# Legacy MS-CFB / OLE2 compound-file signature — the container format behind
# pre-2007 .doc/.xls/.ppt. There is no parser for it anywhere in this repo
# (see `historical_corpus/parsers/docx.py`'s docstring — not worth pulling in
# antiword/textract/libreoffice for the handful of files this affects), but
# it still has to be *recognised* as legacy-office rather than falling
# through to "unknown", both so it's skipped for the right reason and so it
# is never confused with the OOXML zip format below purely on the strength
# of a `.doc` extension — issue #140 was exactly that confusion, the other
# direction.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Best-effort stream-name markers inside an OLE2 compound file, used only to
# guess "doc" vs "xls" for logging/metadata — encoded UTF-16LE because
# that's how the CFB directory stores stream names. Neither format is
# actually parsed, so a wrong guess here has no functional consequence.
_OLE2_XLS_MARKER = "Workbook".encode("utf-16-le")
_OLE2_DOC_MARKER = "WordDocument".encode("utf-16-le")
_OLE2_SNIFF_BYTES = 65536


def _sniff_ole2_kind(path: Path) -> str:
    """Best-effort "doc" vs "xls" label for a legacy MS-CFB file.

    Neither is parsed, so this only has to be good enough for logging /
    skip-reason text — it reads a larger chunk than `_SNIFF_BYTES` because
    the CFB directory sector holding the stream names isn't necessarily in
    the first 2KB. Defaults to "doc": that's the shape issue #140 is
    actually about, and it's also this codebase's pre-existing default for
    an unparsed legacy office file.
    """
    try:
        with path.open("rb") as f:
            head = f.read(_OLE2_SNIFF_BYTES)
    except OSError:
        return "doc"
    if _OLE2_XLS_MARKER in head and _OLE2_DOC_MARKER not in head:
        return "xls"
    return "doc"


def _sniff_zip_kind(path: Path) -> str:
    """Refine a generic `PK\\x03\\x04` zip into "docx" / "xlsx" / "zip" by
    looking at its actual member list rather than trusting a filename or a
    declared mime_type.

    This is the fix for issue #140: a file labelled `.doc` (or
    `application/msword`) that is really OOXML (a zip containing
    `word/document.xml`) used to be routed by that label and fail every
    parser; now the real container tells us which one it is. Any error
    opening it as a zip (corrupt file, or a zip that just isn't an Office
    document) falls back to the generic "zip" kind rather than raising —
    sniffing must never crash the caller.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return "zip"
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return "zip"


def sniff_document_kind(path: Path) -> str:
    """Return "pdf" | "docx" | "xlsx" | "doc" | "xls" | "zip" | "unknown",
    sniffed from `path`'s real bytes — never its extension and never any
    caller-supplied label (a mime_type, say).

    `doc`/`xls` are legacy MS-CFB (OLE2) — recognised, not parsed. `docx`/
    `xlsx` are verified by opening the zip and checking its member list, not
    merely by the `PK\\x03\\x04` prefix. "unknown" covers anything without
    one of these container signatures (audio/image/text/etc. — this
    function only knows about document formats; `inbox.scan.sniff_kind`
    covers the rest and delegates to this one for its own document cases).
    """
    try:
        with path.open("rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return "unknown"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(_OLE2_MAGIC):
        return _sniff_ole2_kind(path)
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip_kind(path)
    return "unknown"
