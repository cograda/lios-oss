"""MCP tool handlers for `signals` (unit tier — `mock_session`).

`watch_test`'s offline mode is the one that matters most here: it lets the
milk question be dry-run against real fixture frames (a real milkman clip,
downscaled — `tests/fixtures/signals/`) with NO camera and NO scheduled
window, which is the whole point per the brief ("run the milk question
against these fixtures before Sunday"). The vision CALL itself is still
mocked (no network in a unit test) — only the image-prep (crop/downscale,
real ffmpeg) runs for real.
"""

from __future__ import annotations

import shutil

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.signals import tools as signals_tools

FIXTURES = Path(__file__).parent / "fixtures" / "signals"
BASELINE = FIXTURES / "milk_baseline_empty.jpg"
CANDIDATE = FIXTURES / "milk_candidate_delivered.jpg"


def test_signals_recent_handler_shape(mock_session):
    mock_session._query_mock.order_by.return_value = mock_session._query_mock
    mock_session._query_mock.limit.return_value = mock_session._query_mock
    mock_session._query_mock.all.return_value = []

    out = json.loads(signals_tools.signals_recent_handler(mock_session, {}))
    assert out == {"events": []}


def test_signals_recent_device_key_filter_normalizes(mock_session):
    """A caller filtering by a colon-form MAC still matches the colonless
    stored form — the filter goes through the same `normalize_device_key`
    as ingestion does."""
    mock_session._query_mock.order_by.return_value = mock_session._query_mock
    mock_session._query_mock.limit.return_value = mock_session._query_mock
    mock_session._query_mock.all.return_value = []

    signals_tools.signals_recent_handler(mock_session, {"device_key": "8C:ED:E1:72:F4:13"})

    # The filter() call's arg is a SQLAlchemy binary expression comparing
    # SignalEvent.device_key to the normalised value.
    (call_args,), _ = mock_session._query_mock.filter.call_args
    assert call_args.right.value == "8cede172f413"


def test_event_row_includes_event_link_when_present():
    from datetime import datetime, timezone

    from app.integrations.signals.models import SignalEvent

    event = SignalEvent(
        id=1, source="protect", kind="person", device_key="8cede172f413",
        device_name="front_door", occurred_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc), sender_event_id="evt-1",
        payload={"alarm": {"eventLocalLink": "https://192.168.1.1/protect/evt-1",
                            "eventPath": "/protect/events/event/evt-1"}},
    )
    row = signals_tools._event_row(event)
    assert row["event_link"] == "https://192.168.1.1/protect/evt-1"


def test_event_row_event_link_falls_back_to_event_path():
    from datetime import datetime, timezone

    from app.integrations.signals.models import SignalEvent

    event = SignalEvent(
        id=1, source="protect", kind="person", device_key=None, device_name=None,
        occurred_at=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc),
        sender_event_id=None,
        payload={"alarm": {"eventPath": "/protect/events/event/evt-2"}},
    )
    row = signals_tools._event_row(event)
    assert row["event_link"] == "/protect/events/event/evt-2"


def test_event_row_event_link_none_when_absent():
    from datetime import datetime, timezone

    from app.integrations.signals.models import SignalEvent

    event = SignalEvent(
        id=1, source="protect", kind="unknown", device_key=None, device_name=None,
        occurred_at=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc),
        sender_event_id=None, payload={"garbage": True},
    )
    row = signals_tools._event_row(event)
    assert row["event_link"] is None


def test_watch_history_handler_shape(mock_session):
    mock_session._query_mock.order_by.return_value = mock_session._query_mock
    mock_session._query_mock.limit.return_value = mock_session._query_mock
    mock_session._query_mock.all.return_value = []

    out = json.loads(signals_tools.watch_history_handler(mock_session, {"watcher": "milk"}))
    assert out == {"runs": []}


def test_watch_confirm_missing_run(mock_session):
    mock_session.get.return_value = None
    out = json.loads(signals_tools.watch_confirm_handler(mock_session, {"run_id": 9999, "correct": True}))
    assert "error" in out


def test_watch_confirm_records_grade(mock_session, monkeypatch):
    from app.integrations.signals.models import WatchRun

    run = WatchRun(
        id=1, watcher="milk", night_date="2026-09-10", opened_at=None,
        status="detected", checks=1,
    )
    mock_session.get.return_value = run
    monkeypatch.setattr(signals_tools, "current_user_id", lambda: 1)

    out = json.loads(signals_tools.watch_confirm_handler(
        mock_session, {"run_id": 1, "correct": False, "note": "false positive, a cat"},
    ))

    assert out["ok"] is True
    assert run.confirmed is False
    assert run.confirmed_by_user_id == 1
    assert run.note == "false positive, a cat"
    mock_session.commit.assert_called_once()


@pytest.mark.skipif(not BASELINE.exists() or not CANDIDATE.exists(), reason="fixture frames not present")
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs the ffmpeg binary (installed in CI and in the server image)")
def test_watch_test_offline_mode_runs_the_real_milk_question(mock_session, tmp_path):
    """Real ffmpeg does the ROI crop/downscale against the real fixture
    frames; only the vision call itself is mocked (no network)."""
    fake_vision = MagicMock()
    fake_vision.compare.return_value = {
        "answer": True, "confidence": 0.88, "where": "bottom centre", "model": "fake",
    }

    with patch("app.plugin.capabilities.get_capability", return_value=fake_vision):
        out = json.loads(signals_tools.watch_test_handler(mock_session, {
            "baseline_path": str(BASELINE),
            "candidate_path": str(CANDIDATE),
            "roi_bottom_fraction": 0.3,
        }))

    assert "error" not in out
    assert out["result"]["answer"] is True
    # The default question (the real milk question) was used since none was
    # supplied — spot-check it mentions the bottom-edge geometry.
    call_args = fake_vision.compare.call_args
    images, question = call_args[0][0], call_args[0][1]
    assert len(images) == 4  # ctx+roi for both baseline and candidate
    assert "bottom edge" in question.lower() or "bottom" in question.lower()
    for img in images:
        assert Path(img).exists()


def test_watch_test_offline_mode_requires_both_paths(mock_session):
    out = json.loads(signals_tools.watch_test_handler(mock_session, {"baseline_path": str(BASELINE)}))
    assert "error" in out


def test_watch_test_offline_mode_rejects_missing_files(mock_session):
    out = json.loads(signals_tools.watch_test_handler(mock_session, {
        "baseline_path": "/no/such/file.jpg", "candidate_path": "/no/such/file2.jpg",
    }))
    assert "error" in out


def test_watch_test_live_mode_requires_camera(mock_session):
    out = json.loads(signals_tools.watch_test_handler(mock_session, {}))
    assert "error" in out


def test_mcp_tools_registers_all_four():
    names = {t["name"] for t in signals_tools.mcp_tools()}
    assert names == {"signals_recent", "watch_history", "watch_confirm", "watch_test"}


def test_read_only_annotations_correct():
    tools = {t["name"]: t for t in signals_tools.mcp_tools()}
    assert tools["signals_recent"]["annotations"]["readOnlyHint"] is True
    assert tools["watch_history"]["annotations"]["readOnlyHint"] is True
    assert tools["watch_confirm"]["annotations"]["readOnlyHint"] is False
    assert tools["watch_test"]["annotations"]["readOnlyHint"] is False
