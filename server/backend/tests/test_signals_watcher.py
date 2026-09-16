"""`Watcher.check()`/`close_run()` control flow (unit tier, no DB/network).

Every IO seam (`grab`, `ask_vision`, `_send_notification`, and module-level
`imaging.mean_brightness`) is monkeypatched — these tests are about the
STATE MACHINE (debounce, brightness-retry-then-ask, close/notify-on-close
opt-in), not about ffmpeg or Gemini.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.integrations.signals.models import WatchRun
from app.integrations.signals.watchers.base import Watcher

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _watcher(**overrides) -> Watcher:
    defaults = dict(
        name="milk",
        camera="front_door",
        weekdays=frozenset({6, 1, 3}),
        start=time(20, 30),
        end=time(0, 30),
        question="is there milk?",
        confidence_threshold=0.7,
        roi_bottom_fraction=0.3,
        brightness_ratio_threshold=1.6,
        brightness_retry_delay=30,
        detected_title="Milk 🥛",
        detected_message="Milk has been delivered — take it in.",
        notify_on_close=False,
    )
    defaults.update(overrides)
    return Watcher(**defaults)


def _run(**overrides) -> WatchRun:
    run = WatchRun(
        watcher="milk", night_date="2026-09-10", opened_at=None,
        status="watching", checks=0,
    )
    for k, v in overrides.items():
        setattr(run, k, v)
    return run


async def test_check_is_a_noop_once_detected(monkeypatch):
    watcher = _watcher()
    run = _run(status="detected")
    session = MagicMock()

    grab_called = False

    def fake_grab(*a, **kw):
        nonlocal grab_called
        grab_called = True
        return Path("/tmp/never.jpg")

    monkeypatch.setattr(watcher, "grab", fake_grab)
    await watcher.check(session, run, wait_settle=False)

    assert not grab_called
    session.commit.assert_not_called()


async def test_check_detects_and_notifies_once(monkeypatch, tmp_path):
    watcher = _watcher(roi_bottom_fraction=None)  # skip ROI/brightness plumbing for this test
    baseline = tmp_path / "baseline.jpg"
    baseline.write_bytes(b"x")
    run = _run(baseline_path=str(baseline))
    session = MagicMock()

    monkeypatch.setattr(watcher, "grab", lambda *a, **kw: tmp_path / "candidate.jpg")
    (tmp_path / "candidate.jpg").write_bytes(b"y")
    monkeypatch.setattr(
        watcher, "build_compare_images",
        lambda b, c: ([b, c], "1) earlier; 2) now"),
    )
    monkeypatch.setattr(
        watcher, "ask_vision",
        lambda images, order: {"answer": True, "confidence": 0.9, "where": "step", "model": "fake"},
    )
    sent = {}
    monkeypatch.setattr(
        watcher, "_send_notification",
        lambda title, message, **kw: sent.update(title=title, message=message, **kw),
    )

    await watcher.check(session, run, wait_settle=False)

    assert run.status == "detected"
    assert run.confidence == 0.9
    assert run.checks == 1
    assert sent["title"] == "Milk 🥛"
    assert sent["message"] == "Milk has been delivered — take it in."
    session.commit.assert_called()


async def test_check_low_confidence_stays_watching(monkeypatch, tmp_path):
    watcher = _watcher(roi_bottom_fraction=None)
    run = _run(baseline_path=str(tmp_path / "baseline.jpg"))
    (tmp_path / "baseline.jpg").write_bytes(b"x")
    session = MagicMock()

    monkeypatch.setattr(watcher, "grab", lambda *a, **kw: tmp_path / "candidate.jpg")
    (tmp_path / "candidate.jpg").write_bytes(b"y")
    monkeypatch.setattr(watcher, "build_compare_images", lambda b, c: ([b, c], "order"))
    monkeypatch.setattr(
        watcher, "ask_vision",
        lambda images, order: {"answer": True, "confidence": 0.4, "where": None, "model": "fake"},
    )
    notified = []
    monkeypatch.setattr(watcher, "_send_notification", lambda *a, **kw: notified.append(1))

    await watcher.check(session, run, wait_settle=False)

    assert run.status == "watching"
    assert not notified


async def test_brightness_retry_waits_and_regrabs(monkeypatch, tmp_path):
    """A candidate whose bottom strip reads much brighter than the baseline
    (hi-vis person on the step) triggers one wait-and-regrab before vision
    is asked at all."""
    watcher = _watcher()
    baseline = tmp_path / "baseline.jpg"
    baseline.write_bytes(b"x")
    run = _run(baseline_path=str(baseline))
    session = MagicMock()

    grabs = [tmp_path / "candidate1.jpg", tmp_path / "candidate2.jpg"]
    for g in grabs:
        g.write_bytes(b"y")
    grab_calls = []

    def fake_grab(night_date, *, label):
        grab_calls.append(label)
        return grabs[len(grab_calls) - 1]

    monkeypatch.setattr(watcher, "grab", fake_grab)

    # First measurement pair (baseline, candidate1) is far brighter -> retry.
    # Second measurement pair (baseline, candidate2) is back to normal.
    brightness_calls = []

    def fake_brightness(path, *, bottom_fraction=None):
        brightness_calls.append(path)
        if path == grabs[0]:
            return 200.0  # candidate1: bright (hi-vis)
        if path == grabs[1]:
            return 40.0  # candidate2: back to normal
        return 30.0  # baseline

    monkeypatch.setattr("app.integrations.signals.watchers.base.imaging.mean_brightness", fake_brightness)

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.integrations.signals.watchers.base.asyncio.sleep", fake_sleep)

    monkeypatch.setattr(watcher, "build_compare_images", lambda b, c: ([b, c], "order"))
    used_candidate = {}
    monkeypatch.setattr(
        watcher, "ask_vision",
        lambda images, order: used_candidate.setdefault("candidate", images[-1])
        and {"answer": False, "confidence": 0.1, "where": None, "model": "fake"},
    )

    await watcher.check(session, run, wait_settle=False)

    assert len(grab_calls) == 2  # initial grab + one re-grab after the wait
    assert 30 in slept  # brightness_retry_delay
    assert run.frame_path == str(grabs[1])  # vision was asked about the SECOND (calmer) grab


async def test_close_run_no_notify_by_default(monkeypatch):
    watcher = _watcher(notify_on_close=False)
    run = _run(status="watching")
    session = MagicMock()
    notified = []
    monkeypatch.setattr(watcher, "_send_notification", lambda *a, **kw: notified.append(1))

    watcher.close_run(session, run)

    assert run.status == "none"
    assert not notified


async def test_close_run_notifies_when_opted_in(monkeypatch):
    watcher = _watcher(notify_on_close=True, close_title="Milk 🥛", close_message="No milk tonight.")
    run = _run(status="watching")
    session = MagicMock()
    sent = {}
    monkeypatch.setattr(watcher, "_send_notification", lambda title, message, **kw: sent.update(title=title, message=message))

    watcher.close_run(session, run)

    assert run.status == "none"
    assert sent == {"title": "Milk 🥛", "message": "No milk tonight."}


async def test_close_run_is_a_noop_if_already_decided():
    watcher = _watcher(notify_on_close=True)
    run = _run(status="detected")
    session = MagicMock()

    watcher.close_run(session, run)

    assert run.status == "detected"  # unchanged
    session.commit.assert_not_called()
