"""Read the transcript Apple already embedded in a recording.

macOS/iOS Voice Memos store each memo's on-device transcript **inside the audio
file**, not in any database — a custom QuickTime user-data atom at
`moov > trak > udta > tsrp`, holding UTF-8 JSON. `CloudRecordings.db` alongside
the files carries only metadata (date, duration, title).

Reading it is free and instant, so it's always worth checking before paying for
a transcription. Two things make it unreliable enough that it can't be the only
source:

  - Memos **synced from another device** (e.g. an iPhone) usually arrive without
    a `tsrp` atom at all, so they have no embedded transcript to find.
  - Older memos predate on-device transcription entirely, and some carry an
    empty placeholder atom rather than no atom — which is why callers must treat
    empty text as absent rather than as "transcribed to nothing".

Ported from `sandbox/voice-memos/extract.py`, which reverse-engineered this. The
atom walk and both `attributedString` shapes are that script's findings; keeping
the derivation here means the sandbox copy can eventually go away.
"""

from __future__ import annotations

import json
import logging
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

# Atoms whose payload is a sequence of child atoms rather than leaf data.
# `tsrp` lives at moov/trak/udta, so the walk has to recurse through those.
_CONTAINER_ATOMS = {b"moov", b"trak", b"udta", b"mdia", b"minf", b"stbl", b"meta"}

# Refuse to walk absurdly large files into memory. A voice memo is megabytes;
# anything past this is not what this function is for.
_MAX_BYTES = 200 * 1024 * 1024


def _walk_atoms(data: bytes, start: int, end: int):
    """Yield (type, payload_start, payload_end), recursing into containers."""
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8]
        hdr = 8
        if size == 1:  # 64-bit extended size follows the type
            if i + 16 > end:
                return
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:  # "extends to end of container"
            size = end - i
        # A size smaller than its own header means a malformed file; bail rather
        # than loop forever on a zero/negative advance.
        if size < hdr:
            return
        yield typ, i + hdr, min(i + size, end)
        if typ in _CONTAINER_ATOMS:
            # `meta` uniquely prefixes its children with a version/flags word.
            sub = i + hdr + (4 if typ == b"meta" else 0)
            yield from _walk_atoms(data, sub, min(i + size, end))
        i += size


def _text_from_attributed_string(ats) -> str:
    """Join the plain-text runs out of Apple's attributed-string JSON.

    Two shapes in the wild, both interleaving strings with attribute data:
        separated:   {"runs": [str, attrIdx, str, attrIdx, ...]}
        interleaved: [str, {attrs}, str, {attrs}, ...]
    Either way the strings are the transcript and everything else is styling.
    """
    if isinstance(ats, dict):
        runs = ats.get("runs", [])
    elif isinstance(ats, list):
        runs = ats
    else:
        return ""
    return "".join(x for x in runs if isinstance(x, str)).strip()


def read_embedded_transcript(path: Path) -> tuple[str | None, str | None]:
    """Return (text, locale) from the file's `tsrp` atom.

    `text` is None when there is no atom, and "" when the atom exists but is an
    empty placeholder — callers should treat both as "no transcript available",
    but the distinction is preserved because it's the difference between "this
    device never transcribed it" and "it tried and produced nothing".

    Never raises: a malformed container is a reason to fall back to a real
    transcription, not to fail the caller's job.
    """
    try:
        if path.stat().st_size > _MAX_BYTES:
            logger.warning("[transcription] %s too large to scan for tsrp", path.name)
            return None, None
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("[transcription] cannot read %s: %s", path.name, exc)
        return None, None

    try:
        for typ, payload_start, payload_end in _walk_atoms(data, 0, len(data)):
            if typ != b"tsrp":
                continue
            blob = data[payload_start:payload_end].decode("utf-8", errors="replace")
            parsed = json.loads(blob)
            attributed = parsed.get("attributedString", parsed)
            locale = (parsed.get("locale") or {}).get("identifier")
            return _text_from_attributed_string(attributed), locale
    except (struct.error, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("[transcription] malformed tsrp in %s: %s", path.name, exc)
        return None, None

    return None, None
