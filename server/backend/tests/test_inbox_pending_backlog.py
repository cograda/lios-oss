"""Regression test for lios#184 — a freshly-captured item invisible to
`inbox_pending` (and reported "not found" by `inbox_preview`) despite a
successful ingest.

Root cause: `scan.iter_pending_files()` sorts pending files **oldest first**
(by design — see its docstring, "so triage tackles backlog in arrival
order"), but `scan.list_pending()` then took `iter_pending_files(user_id)
[:limit]` — the first `limit` items of an oldest-first list, i.e. the
*oldest* items in the backlog, not the most recent ones. `inbox_pending`'s
default `limit` is 20 (see `tools.py::handle_pending`), so once a user's
pending backlog holds more than 20 items, every item captured after the
20th-oldest is silently excluded from every `inbox_pending` call, forever —
with no error, and no indication a backlog even exists — until enough of the
older items are cleared out. This is exactly the reported symptom: a captured
item (confirmed via the ingest confirmation) never appearing across four
separate `inbox_pending` calls in one session.
"""

from __future__ import annotations

import json

import pytest

from app.integrations.inbox import scan


@pytest.fixture
def fake_inbox_root(tmp_path, monkeypatch):
    """Point `scan.inbox_root()` at a throwaway directory (mirrors the
    fixture of the same name in test_inbox_scoping.py)."""
    monkeypatch.setattr(scan.settings, "inbox_path", str(tmp_path))
    return tmp_path


def _drop_ordered(root, user_id: int, bucket: str, ts_prefix: str, text: str):
    """Write a pending file whose name sorts by `ts_prefix`, matching the
    real `YYYYMMDD-HHMMSS-<hex>` ingest naming scheme closely enough for
    `iter_pending_files`'s name-sort to behave the same way."""
    d = scan.user_root(user_id) / bucket
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ts_prefix}-cafe1234"
    p.write_text(text)
    scan.write_sidecar(p, {})
    return p


class TestPendingBacklogNeverHidesANewCapture:
    def test_newest_capture_is_returned_even_behind_a_large_backlog(
        self, fake_inbox_root
    ):
        # 25 older pending items, all dated strictly before the new capture
        # below — one more than the default `limit=20` used by the
        # `inbox_pending` MCP tool.
        for i in range(25):
            _drop_ordered(
                fake_inbox_root, 1, "file", f"202601{i+1:02d}-000000",
                f"old item {i}",
            )

        newest = _drop_ordered(
            fake_inbox_root, 1, "file", "20260908-081724", "Morning Workout export"
        )

        items = scan.list_pending(1, limit=20)

        assert any(item["path"] == str(newest) for item in items), (
            "the most recently captured item must always be visible in "
            "inbox_pending, regardless of how large the existing backlog is"
        )

    def test_default_tool_limit_reproduces_the_reported_bug(self, fake_inbox_root):
        """Same scenario at the exact default `limit` the MCP tool uses."""
        for i in range(25):
            _drop_ordered(
                fake_inbox_root, 1, "file", f"202601{i+1:02d}-000000",
                f"old item {i}",
            )
        newest = _drop_ordered(
            fake_inbox_root, 1, "file", "20260908-081724", "Morning Workout export"
        )

        items = scan.list_pending(1, limit=20)  # tools.py::handle_pending's default
        paths = [item["path"] for item in items]

        assert str(newest) in paths


class TestToolReportsTruncation:
    """`inbox_pending` is a page, and must say so: `total_pending` and
    `truncated` let a caller tell 20-of-25 from 20-of-20 (lios#184)."""

    def test_handle_pending_reports_total_and_truncated(
        self, fake_inbox_root, mock_session, monkeypatch
    ):
        from app.auth.context import use_user
        from app.integrations.inbox.tools import handle_pending

        # Inline enrichment is not under test and would need a DB.
        monkeypatch.setattr(scan, "enrich_one", lambda p: None)
        for i in range(25):
            _drop_ordered(
                fake_inbox_root, 1, "file", f"202601{i+1:02d}-000000", f"old item {i}"
            )

        with use_user(1):
            out = json.loads(handle_pending(mock_session, {"limit": 20}))

        assert out["count"] == 20
        assert out["total_pending"] == 25
        assert out["truncated"] is True

        with use_user(1):
            out = json.loads(handle_pending(mock_session, {"limit": 50}))

        assert out["count"] == 25
        assert out["total_pending"] == 25
        assert out["truncated"] is False
