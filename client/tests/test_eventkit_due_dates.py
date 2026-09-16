"""`ReminderStore._parse_due_date` — offset handling.

Two bugs shipped here until 2026-08-23 and both were about the offset, not the
parsing: a timezone-aware string matched none of the three `%z`-less formats
and was silently dropped (reminder created with NO alarm), and naive input was
forced to UTC rather than local, so every naive summer time fired an hour late.

`Foundation` is imported *inside* the function, so these tests inject a fake
NSDate and assert on the epoch seconds handed to it — that number IS the bug.
No PyObjC needed, which also means these run in CI, where the real fault would
never have been caught because CI has no EventKit at all.

TZ is pinned to Europe/Dublin (UTC+1 in August, UTC in December) because the
whole point is that local-vs-UTC is not the same thing for eight months a year.
"""

import importlib
import os
import sys
import time
import types
from datetime import datetime, timezone

import pytest


@pytest.fixture
def parse(monkeypatch):
    """Yield a callable returning the epoch seconds passed to NSDate (or None)."""
    captured: list[float] = []

    fake = types.ModuleType("Foundation")

    class _NSDate:
        @staticmethod
        def dateWithTimeIntervalSince1970_(ts):
            captured.append(ts)
            return f"NSDate({ts})"

    fake.NSDate = _NSDate
    monkeypatch.setitem(sys.modules, "Foundation", fake)

    monkeypatch.setenv("TZ", "Europe/Dublin")
    time.tzset()

    from lios_sync.eventkit import ReminderStore

    def _parse(s):
        captured.clear()
        result = ReminderStore._parse_due_date(s)
        return None if result is None else captured[0]

    yield _parse

    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def _epoch(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


class TestNaiveIsLocalNotUTC:
    def test_naive_summer_noon_is_local_noon_not_utc_noon(self, parse):
        """The hour-late bug. Dublin is UTC+1 in August, so a caller asking for
        noon must land on 11:00Z. The old code produced 12:00Z = 13:00 local."""
        assert parse("2026-08-23T12:00:00") == _epoch(2026, 8, 23, 11)

    def test_naive_winter_noon_is_unchanged_because_dublin_is_utc_then(self, parse):
        """Guards against 'fixing' this with a hardcoded -1: in December Dublin
        IS UTC, so local noon and 12:00Z coincide. A constant offset would
        break this case while passing the summer one."""
        assert parse("2026-12-23T12:00:00") == _epoch(2026, 12, 23, 12)

    def test_date_only_is_local_midnight(self, parse):
        assert parse("2026-08-23") == _epoch(2026, 8, 22, 23)


class TestAwareIsHonouredNotDropped:
    def test_offset_string_is_parsed_at_all(self, parse):
        """The silent-drop bug: this returned None, so the reminder was created
        with no due date and no alarm. Assert non-None *before* the value, so a
        regression reads as 'dropped' rather than 'wrong'."""
        assert parse("2026-08-23T12:00:00+01:00") is not None

    def test_offset_is_converted_not_clobbered(self, parse):
        """+01:00 noon is 11:00Z. If someone reintroduces
        `.replace(tzinfo=utc)` the offset is overwritten rather than applied
        and this yields 12:00Z — the 'looks like it works' failure mode."""
        assert parse("2026-08-23T12:00:00+01:00") == _epoch(2026, 8, 23, 11)

    def test_zulu_suffix_is_utc(self, parse):
        assert parse("2026-08-23T12:00:00Z") == _epoch(2026, 8, 23, 12)

    def test_a_non_local_offset_is_respected(self, parse):
        """Not every caller is in Dublin: +09:00 noon is 03:00Z."""
        assert parse("2026-08-23T12:00:00+09:00") == _epoch(2026, 8, 23, 3)


class TestFallbacksAndFailure:
    def test_minute_precision_still_works(self, parse):
        assert parse("2026-08-23T12:00") == _epoch(2026, 8, 23, 11)

    def test_surrounding_whitespace_is_tolerated(self, parse):
        assert parse("  2026-08-23T12:00:00  ") == _epoch(2026, 8, 23, 11)

    def test_unparseable_returns_none_rather_than_raising(self, parse):
        """Degrade, never raise — an exception here kills the whole EventKit
        command, losing the reminder entirely rather than just its alarm."""
        assert parse("next Tuesday-ish") is None
