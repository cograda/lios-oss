"""Unit tests for app/tools/helpers.py's timestamp-age helpers.

`ensure_utc`/`age_seconds` (Chunk G of the 2026-07 improvement round) replace
the hand-rolled "attach tzinfo if naive, then diff against now-UTC" dance
that was copy-pasted across commute/tools.py and system/tools.py.
"""

from datetime import datetime, timedelta, timezone

from app.tools.helpers import age_seconds, ensure_utc


class TestEnsureUtc:
    def test_none_passes_through(self):
        assert ensure_utc(None) is None

    def test_naive_gets_utc_attached(self):
        naive = datetime(2026, 7, 14, 12, 0, 0)
        result = ensure_utc(naive)
        assert result.tzinfo == timezone.utc
        assert result.replace(tzinfo=None) == naive

    def test_aware_utc_unchanged(self):
        aware = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        assert ensure_utc(aware) == aware

    def test_aware_non_utc_converted(self):
        tz = timezone(timedelta(hours=1))
        aware = datetime(2026, 7, 14, 13, 0, 0, tzinfo=tz)
        result = ensure_utc(aware)
        assert result.tzinfo == timezone.utc
        assert result == datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


class TestAgeSeconds:
    def test_none_returns_none(self):
        assert age_seconds(None) is None

    def test_naive_treated_as_utc(self):
        now = datetime(2026, 7, 14, 12, 5, 0, tzinfo=timezone.utc)
        naive = datetime(2026, 7, 14, 12, 0, 0)
        assert age_seconds(naive, now=now) == 300.0

    def test_aware_diffed_directly(self):
        now = datetime(2026, 7, 14, 12, 5, 0, tzinfo=timezone.utc)
        aware = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        assert age_seconds(aware, now=now) == 300.0

    def test_aware_non_utc_normalised_before_diff(self):
        now = datetime(2026, 7, 14, 12, 5, 0, tzinfo=timezone.utc)
        tz = timezone(timedelta(hours=1))
        aware = datetime(2026, 7, 14, 13, 0, 0, tzinfo=tz)  # == 12:00 UTC
        assert age_seconds(aware, now=now) == 300.0

    def test_defaults_now_to_current_utc(self):
        dt = datetime.now(timezone.utc) - timedelta(seconds=10)
        result = age_seconds(dt)
        assert 9.0 <= result <= 20.0
