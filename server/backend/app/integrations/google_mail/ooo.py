"""Out-of-office / auto-reply detection for mail scoring (lios#143).

`/kickoff`'s email tiering (the Track C2 subagent — see
`app/prompts/templates/kickoff.md.j2`) uses `gmail_recent`/`gmail_search`
output to decide whether to down-weight a sender as "away, don't expect a
reply". That signal must not outlive the auto-reply it came from: an
"out of office until 15 April" read on 20 April is not current intel — the
scoring was treating stale evidence as live (lios#143).

This is computed **on the fly**, from the message itself; nothing is
persisted, so there's nothing to keep in sync or expire out of a table —
every check re-derives from `subject`/`snippet`/`date` at read time.

Two ways an OOO reply goes stale:
  1. It names a return date, and that date has passed -> expired.
  2. It names no date at all, so it falls back to a bounded window
     (`OOO_DEFAULT_WINDOW_DAYS`) measured from when the auto-reply was sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# Bounded window for an auto-reply with no stated return date. Long enough to
# cover a two-week holiday reply that never names a date, short enough that a
# six-week-old reply can no longer be read as "they're away right now".
OOO_DEFAULT_WINDOW_DAYS = 14

_OOO_PHRASES = re.compile(
    r"out[\s-]of[\s-]office"
    r"|automatic reply"
    r"|auto[\s-]reply"
    r"|vacation responder"
    r"|i(?:'m| am) (?:currently )?(?:out of office|away from|on leave|on annual leave)",
    re.IGNORECASE,
)

_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)

# Deliberately narrow (day + month name, or a numeric d/m date) rather than a
# do-everything NLP date parser — a false negative here just falls back to
# the bounded window, which is the safe direction to fail in.
_DATE_PATTERNS = [
    re.compile(
        rf"\b(?:back|return(?:ing)?|available|until)\s+(?:on\s+)?(?:\w+\s+)?"
        rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTHS})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:back|return(?:ing)?|available|until)\s+(?:on\s+)?"
        r"(?P<num_day>\d{1,2})[/-](?P<num_month>\d{1,2})(?:[/-](?P<num_year>\d{2,4}))?\b",
        re.IGNORECASE,
    ),
]

_MONTH_LOOKUP: dict[str, int] = {}
for _i, _names in enumerate(
    (
        ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
        ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
        ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
    ),
    start=1,
):
    for _name in _names:
        _MONTH_LOOKUP[_name] = _i


@dataclass(frozen=True)
class OOOStatus:
    """Result of checking one message for a still-active OOO auto-reply."""

    active: bool
    return_date: date | None = None
    source_date: datetime | None = None
    reason: str = ""


def _parse_return_date(text: str, reference: datetime) -> date | None:
    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groupdict()
        try:
            if groups.get("month"):
                month = _MONTH_LOOKUP.get(groups["month"][:3].lower())
                if not month:
                    continue
                day = int(groups["day"])
                year = reference.year
                candidate = date(year, month, day)
                # "until 15 April" read in December means next April, not one
                # that already passed this year — but only roll forward, never
                # invent a past date for a reply that just arrived.
                if candidate < reference.date() - timedelta(days=OOO_DEFAULT_WINDOW_DAYS):
                    candidate = date(year + 1, month, day)
                return candidate
            else:
                day = int(groups["num_day"])
                month = int(groups["num_month"])
                year_raw = groups.get("num_year")
                if year_raw:
                    year = int(year_raw)
                    if year < 100:
                        year += 2000
                else:
                    year = reference.year
                return date(year, month, day)
        except ValueError:
            continue
    return None


def detect_ooo(
    subject: str | None,
    snippet: str | None,
    sent_at: datetime | None,
    *,
    now: datetime | None = None,
    window_days: int = OOO_DEFAULT_WINDOW_DAYS,
) -> OOOStatus:
    """Is this message evidence of a sender who is *currently* out of office?

    Returns `active=False` for anything that isn't an auto-reply at all, and
    also for an auto-reply whose evidence has gone stale — a stated return
    date that has already passed, or (with no stated date) a message older
    than `window_days`. A naive `sent_at` is treated as UTC, matching the
    rest of mail ingestion (`google_mail/sync.py`).
    """
    text = " ".join(part for part in (subject, snippet) if part)
    if not text or not _OOO_PHRASES.search(text):
        return OOOStatus(active=False, reason="not_ooo")

    now = now or datetime.now(timezone.utc)
    if sent_at is None:
        # No timestamp to reason about age from — never trust it, since the
        # whole point is that stale evidence must not read as current.
        return OOOStatus(active=False, reason="no_timestamp")
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)

    return_date = _parse_return_date(text, sent_at)
    if return_date is not None:
        if return_date < now.date():
            return OOOStatus(active=False, return_date=return_date, source_date=sent_at, reason="return_date_passed")
        return OOOStatus(active=True, return_date=return_date, source_date=sent_at, reason="return_date_future")

    # No stated date: bounded window from when the auto-reply was sent.
    if now - sent_at > timedelta(days=window_days):
        return OOOStatus(active=False, source_date=sent_at, reason="window_expired")
    return OOOStatus(active=True, source_date=sent_at, reason="within_window")
