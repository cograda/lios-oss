"""Backfill historical_documents.document_date from filename date patterns.

Most ingested BoQ/recommendation-for-payment xlsx files, Gemini meeting notes, and
builder invoices carry a date in the filename. The parsers missed those because
they only look at file metadata (PDF /CreationDate, xlsx core properties). This
one-shot script walks null-date rows and fills in the latest date it can extract
from source_path.

Run with:
    ssh ubuntu 'docker compose -f ~/comar-server/docker-compose.yml exec -T app \
        python scripts/backfill_historical_dates.py'
"""
from __future__ import annotations

import re
from datetime import date

from app.db import get_db
from app.integrations.historical_corpus.models import HistoricalDocument


# Non-consuming scans. An adjacent invalid-but-matching candidate (e.g. "5 23.03"
# in "No 5 23.03.2026") would otherwise swallow the good date. Lookahead-only
# matching advances one char at a time so every plausible position is tried.
_DMY = re.compile(
    r"(?<!\d)(?=(?P<d>\d{1,2})[.\-_ ](?P<m>\d{1,2})[.\-_ ](?P<y>\d{4}|\d{2})(?!\d))"
)
_YMD = re.compile(
    r"(?<!\d)(?=(?P<y>20\d{2})[.\-_ ](?P<m>\d{1,2})[.\-_ ](?P<d>\d{1,2})(?!\d))"
)

MIN_YEAR, MAX_YEAR = 2020, 2027


def _normalise_year(y: int) -> int:
    if y < 100:
        return 2000 + y  # all two-digit years in this corpus are post-2000
    return y


def _candidates(path: str) -> list[date]:
    out: list[date] = []
    for m in _DMY.finditer(path):
        try:
            d, mo, y = int(m["d"]), int(m["m"]), _normalise_year(int(m["y"]))
            if MIN_YEAR <= y <= MAX_YEAR and 1 <= mo <= 12 and 1 <= d <= 31:
                out.append(date(y, mo, d))
        except ValueError:
            continue
    for m in _YMD.finditer(path):
        try:
            y, mo, d = int(m["y"]), int(m["m"]), int(m["d"])
            if MIN_YEAR <= y <= MAX_YEAR and 1 <= mo <= 12 and 1 <= d <= 31:
                out.append(date(y, mo, d))
        except ValueError:
            continue
    return out


def extract_date(source_path: str) -> date | None:
    """Return the LATEST plausible date found in the filename, else None.

    Latest-wins is the right heuristic here because recommendation-for-payment
    files often carry both a base-BoQ date (e.g. "30.06.25") and the
    recommendation's own issue date (e.g. "23.03.2026"); we care about the
    latter.
    """
    cands = _candidates(source_path)
    return max(cands) if cands else None


def main() -> None:
    db = get_db()
    with db.session() as s:
        rows = (
            s.query(HistoricalDocument)
            .filter(HistoricalDocument.document_date.is_(None))
            .all()
        )
        updated = skipped = 0
        for doc in rows:
            d = extract_date(doc.source_path)
            if d is None:
                skipped += 1
                continue
            doc.document_date = d
            updated += 1
            print(f"[{d.isoformat()}] {doc.source_path}")
        s.commit()
        print(f"\nUpdated {updated}; left {skipped} without an extractable date.")


if __name__ == "__main__":
    main()
