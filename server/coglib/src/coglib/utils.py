"""Small shared utilities."""

from __future__ import annotations

from datetime import date, datetime

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y%m%d",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
]


def parse_date(value: str) -> date:
    """Parse a date string, trying multiple common formats.

    Raises ValueError if none match.
    """
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {value!r}")
