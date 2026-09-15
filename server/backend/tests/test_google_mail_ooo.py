"""Out-of-office / auto-reply expiry (lios#143).

Unit tier: `detect_ooo` is a pure function (no DB, no I/O) — these tests are
the same shape as `test_gmail_attachments.py`'s `_walk_attachment_parts` suite.
"""

from datetime import datetime, timedelta, timezone

from app.integrations.google_mail.ooo import OOO_DEFAULT_WINDOW_DAYS, detect_ooo


def _dt(*args, **kwargs) -> datetime:
    return datetime(*args, tzinfo=timezone.utc, **kwargs)


class TestDetectOOO:
    def test_not_an_auto_reply_is_never_active(self):
        status = detect_ooo("Re: renovation invoice", "Thanks, sending the BER cert now.", _dt(2026, 9, 1))
        assert status.active is False
        assert status.reason == "not_ooo"

    def test_past_return_date_is_ignored(self):
        # Sent well before "now" and the stated return date has already passed.
        sent_at = _dt(2026, 4, 1)
        now = _dt(2026, 9, 7)
        status = detect_ooo(
            "Automatic reply: Out of Office",
            "I am out of office until 15 April and will respond on my return.",
            sent_at,
            now=now,
        )
        assert status.active is False
        assert status.reason == "return_date_passed"
        assert status.return_date == datetime(2026, 4, 15).date()

    def test_future_return_date_applies(self):
        # Sent recently, stated return date is still ahead of "now".
        sent_at = _dt(2026, 9, 5)
        now = _dt(2026, 9, 7)
        status = detect_ooo(
            "Out of Office",
            "I am currently out of office and will be back on 20 September.",
            sent_at,
            now=now,
        )
        assert status.active is True
        assert status.reason == "return_date_future"
        assert status.return_date.month == 9
        assert status.return_date.day == 20

    def test_no_date_expires_after_default_window(self):
        # No return date stated at all; sent well outside the bounded window.
        sent_at = _dt(2026, 8, 1)
        now = sent_at + timedelta(days=OOO_DEFAULT_WINDOW_DAYS + 1)
        status = detect_ooo(
            "Automatic reply",
            "Thanks for your email, I'm away from the office right now.",
            sent_at,
            now=now,
        )
        assert status.active is False
        assert status.reason == "window_expired"

    def test_no_date_still_active_within_default_window(self):
        sent_at = _dt(2026, 9, 1)
        now = sent_at + timedelta(days=OOO_DEFAULT_WINDOW_DAYS - 1)
        status = detect_ooo(
            "Automatic reply",
            "Thanks for your email, I'm away from the office right now.",
            sent_at,
            now=now,
        )
        assert status.active is True
        assert status.reason == "within_window"

    def test_no_timestamp_is_never_trusted(self):
        # No sent_at to reason about age from at all — must not read as active,
        # since the whole bug is stale evidence being trusted as current.
        status = detect_ooo("Out of Office", "I am out of office.", None)
        assert status.active is False
        assert status.reason == "no_timestamp"

    def test_naive_datetime_treated_as_utc(self):
        sent_at = datetime(2026, 9, 5)  # naive
        now = _dt(2026, 9, 6)
        status = detect_ooo("Out of Office", "Back on 20 September.", sent_at, now=now)
        assert status.active is True

    def test_numeric_return_date_format(self):
        sent_at = _dt(2026, 9, 1)
        now = _dt(2026, 9, 7)
        status = detect_ooo(
            "Automatic reply", "Returning 20/09/2026.", sent_at, now=now,
        )
        assert status.active is True
        assert status.return_date == datetime(2026, 9, 20).date()

    def test_custom_window_days_respected(self):
        sent_at = _dt(2026, 9, 1)
        now = sent_at + timedelta(days=3)
        status = detect_ooo(
            "Automatic reply", "I'm away from my desk.", sent_at, now=now, window_days=2,
        )
        assert status.active is False
        assert status.reason == "window_expired"
