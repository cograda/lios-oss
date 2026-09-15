"""Bounded retry for a failed vision call (issue #147).

Before this, `describe_pending` stamped `described_at` on the very first
attempt regardless of outcome (see `vision/facade.py::VisionResult`'s own
docstring and `core/CLAUDE.md`'s Known Issues — "described_at is set even on
failure"). A provider error, timeout, or 429 therefore buried an image's
description forever: the idempotency check that skips already-`described_at`
files could never tell "genuinely finished" from "failed once, five minutes
after deploy". Nothing ever tried again.

This file covers the two new pieces directly: the retry-selection rule
(`_vision_ready_for_retry` / `_vision_backoff_minutes`) and the give-up path
in `describe_pending` once `MAX_VISION_ATTEMPTS` is reached. The end-to-end
sweep behaviour (a single failure retries, repeated failures give up and
announce) lives alongside the rest of `describe_pending`'s tests in
`test_inbox_enrichment.py::TestDescribePendingAnnounces`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.inbox import scan


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestVisionReadyForRetry:
    """The pure selection rule: no vision result yet, attempts under the cap,
    and — once it has failed at least once — long enough since the last
    attempt."""

    def test_never_attempted_is_ready_immediately(self):
        assert scan._vision_ready_for_retry({}) is True

    def test_described_is_never_retried(self):
        """A terminal outcome (success OR a permanent give-up) always sets
        `described_at`, and that alone is enough to stop retrying — even if
        `vision_attempts` somehow still looked retryable."""
        meta = {"described_at": "2026-09-07T09:00:00+00:00", "vision_attempts": 1}
        assert scan._vision_ready_for_retry(meta) is False

    def test_freshly_failed_is_not_yet_ready(self):
        """One failure a moment ago must not be retried on the very same
        sweep tick — that's the "hammer a 429" failure mode the backoff
        exists to prevent."""
        now = datetime.now(timezone.utc)
        meta = {"vision_attempts": 1, "vision_attempted_at": _iso(now)}
        assert scan._vision_ready_for_retry(meta, now=now) is False

    def test_ready_once_its_backoff_has_elapsed(self):
        now = datetime.now(timezone.utc)
        backoff = scan._vision_backoff_minutes(1)
        last = now - timedelta(minutes=backoff + 1)
        meta = {"vision_attempts": 1, "vision_attempted_at": _iso(last)}
        assert scan._vision_ready_for_retry(meta, now=now) is True

    def test_not_ready_just_short_of_its_backoff(self):
        now = datetime.now(timezone.utc)
        backoff = scan._vision_backoff_minutes(1)
        last = now - timedelta(minutes=backoff - 1)
        meta = {"vision_attempts": 1, "vision_attempted_at": _iso(last)}
        assert scan._vision_ready_for_retry(meta, now=now) is False

    def test_later_attempts_wait_longer(self):
        """The backoff schedule grows with the attempt count — a second
        failure earns a longer wait than a first, not the same one."""
        assert scan._vision_backoff_minutes(2) > scan._vision_backoff_minutes(1)

    def test_exhausted_attempts_are_never_ready_regardless_of_backoff(self):
        """At `MAX_VISION_ATTEMPTS` failures, no amount of waiting makes it
        retryable again — `describe_pending` is expected to have given up and
        stamped `described_at` by then, but the selection rule holds even if
        that somehow didn't happen."""
        long_ago = datetime.now(timezone.utc) - timedelta(days=365)
        meta = {
            "vision_attempts": scan.MAX_VISION_ATTEMPTS,
            "vision_attempted_at": _iso(long_ago),
        }
        assert scan._vision_ready_for_retry(meta) is False

    def test_missing_timestamp_with_attempts_recorded_retries_rather_than_sticking(self):
        """A data shape that should be impossible (attempts recorded, no
        timestamp) fails toward "try again", not toward "permanently stuck"."""
        meta = {"vision_attempts": 1}
        assert scan._vision_ready_for_retry(meta) is True


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    monkeypatch.setattr(scan.settings, "inbox_path", str(root))
    user_root = scan.user_root(1)
    (user_root / "incoming").mkdir(parents=True)
    return user_root


def _pending_image(inbox, name="20260907-090000-abc123", **meta):
    path = inbox / "incoming" / name
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    scan.write_sidecar(path, {"kind": "image", **meta})
    return path


class _FailingVision:
    """A vision capability that always fails the same way, and counts calls
    so a test can assert exactly how many paid attempts were made."""

    def __init__(self, error="gemini call failed: 503"):
        self.error = error
        self.calls: list = []

    def available(self):
        return True

    def describe(self, path):
        from app.integrations.vision.facade import VisionResult

        self.calls.append(path)
        return VisionResult(summary="", error=self.error, model="gemini-3.7-flash")


class TestDescribePendingRetrySweep:
    """`describe_pending` end to end against `_FailingVision` — the give-up
    path plus the counters a caller/log line depends on."""

    def _patch_vision(self, monkeypatch, vision):
        import app.plugin.capabilities as capabilities

        monkeypatch.setattr(
            capabilities, "get_capability",
            lambda name: vision if name == "vision.image" else (_ for _ in ()).throw(KeyError(name)),
        )

    def test_a_single_failure_is_not_permanent(self, inbox, monkeypatch):
        """The bug this closes: one failed attempt must leave the file
        retryable, not stamp `described_at` and bury it forever."""
        path = _pending_image(inbox)
        vision = _FailingVision()
        self._patch_vision(monkeypatch, vision)

        counts = scan.describe_pending()

        assert counts == {
            "considered": 1, "described": 0, "skipped": 0,
            "failed": 0, "retrying": 1, "deferred": 0,
        }
        meta = scan.read_sidecar(path)
        assert not meta.get("described_at")
        assert meta["vision_attempts"] == 1
        assert meta["vision_attempted_at"]
        assert meta["vision_error"] == vision.error

    def test_a_retrying_failure_is_deferred_inside_its_backoff(self, inbox, monkeypatch):
        """A second sweep run immediately after the first must not spend a
        second paid call — the backoff hasn't elapsed yet."""
        path = _pending_image(inbox)
        vision = _FailingVision()
        self._patch_vision(monkeypatch, vision)

        scan.describe_pending()
        counts = scan.describe_pending()

        assert counts["deferred"] == 1
        assert counts["retrying"] == 0
        assert len(vision.calls) == 1  # not billed twice

    def test_repeated_failures_give_up_after_max_attempts(self, inbox, monkeypatch):
        """Drive it through `MAX_VISION_ATTEMPTS` failures (each one already
        past its own backoff), then assert the give-up shape: `described_at`
        set, `vision_failed` true, `vision_attempts` cleared."""
        path = _pending_image(inbox)
        vision = _FailingVision()
        self._patch_vision(monkeypatch, vision)

        for attempt in range(1, scan.MAX_VISION_ATTEMPTS + 1):
            counts = scan.describe_pending()
            meta = scan.read_sidecar(path)
            if attempt < scan.MAX_VISION_ATTEMPTS:
                assert counts["retrying"] == 1
                assert not meta.get("described_at")
                assert meta["vision_attempts"] == attempt
                # Backdate so the next sweep considers it past backoff,
                # rather than the test sleeping for real minutes.
                meta["vision_attempted_at"] = _iso(
                    datetime.now(timezone.utc) - timedelta(days=1)
                )
                scan.write_sidecar(path, meta)
            else:
                assert counts["failed"] == 1
                assert counts["retrying"] == 0
                assert meta["described_at"]
                assert meta["vision_failed"] is True
                assert "vision_attempts" not in meta

        assert len(vision.calls) == scan.MAX_VISION_ATTEMPTS

        # And now permanently done: a further sweep neither calls vision
        # again nor un-gives-up.
        vision.calls.clear()
        final_counts = scan.describe_pending()
        assert final_counts["skipped"] == 1
        assert vision.calls == []
