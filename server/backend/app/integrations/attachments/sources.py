"""Single declared answer to "which attachment sources can `ingest` actually
handle?" — read by both `scan.py` (to decide pending vs unsupported at
discovery time) and `ingest.py` (to decide whether to attempt a download).

This is the fix, not a detail of it: before this module existed, `scan.py`
queued every Gmail attachment as `pending` while `ingest.py` independently
rejected `source='gmail'` inline — two places deciding the same fact, and
free to disagree. They did: Gmail rows piled up `pending` forever because
nothing downstream could ever consume them. Whichever side changes support
for a source, it now changes here, and the other side sees it for free.

Adding a source (e.g. once the Gmail download path lands) is a one-line
change: add it to this set and `scan_gmail` starts queueing it as `pending`
again, with no other edit needed.
"""

from __future__ import annotations

# Sources `ingest.py` can actually download + parse today.
SUPPORTED_INGEST_SOURCES: frozenset[str] = frozenset({"whatsapp"})


def unsupported_source_reason(source: str) -> str:
    """Canonical skip_reason text for a source `ingest` can't handle yet.

    Kept as one function so scan-time and any future callers produce the
    exact same string — useful for tests and for anything that greps
    skip_reason.
    """
    return f"source {source!r} not supported yet"
