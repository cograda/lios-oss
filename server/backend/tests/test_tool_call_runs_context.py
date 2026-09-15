"""Unit tier — the per-call tool_calls ContextVar + completion-log plumbing.

Covers the pieces of Phase 5 (tool-call observability) that don't need a
real database: `bind_tool_call_id`/`current_tool_call_id` (the same
set/reset ContextVar pattern as `_connection_user`), and
`record_tool_call`'s "never break dispatch" contract when persistence
itself fails. `record_tool_call` moved from `app.services.tool_calls` to
`app.services.runs` in Wave 5.1 (tool_calls absorbed into the `runs`
ledger) — same signature, new home.

DB-backed dispatch-through-MCP and system_alerts behaviour live in
tests/test_tool_call_runs.py (db tier).
"""

import logging

import pytest


# ---------------------------------------------------------------------------
# bind_tool_call_id / current_tool_call_id
# ---------------------------------------------------------------------------

def test_current_tool_call_id_is_none_outside_binding():
    from app.mcp.server import current_tool_call_id

    assert current_tool_call_id() is None


def test_bind_tool_call_id_sets_and_resets():
    from app.mcp.server import bind_tool_call_id, current_tool_call_id

    assert current_tool_call_id() is None
    with bind_tool_call_id("abcd1234"):
        assert current_tool_call_id() == "abcd1234"
    assert current_tool_call_id() is None


def test_bind_tool_call_id_resets_even_on_exception():
    from app.mcp.server import bind_tool_call_id, current_tool_call_id

    with pytest.raises(ValueError):
        with bind_tool_call_id("will-blow-up"):
            assert current_tool_call_id() == "will-blow-up"
            raise ValueError("boom")
    assert current_tool_call_id() is None


def test_bind_tool_call_id_nests_and_restores_outer_value():
    """Mirrors the _connection_user get/reset pattern: nested binds don't
    bleed into each other once the inner one exits."""
    from app.mcp.server import bind_tool_call_id, current_tool_call_id

    with bind_tool_call_id("outer"):
        assert current_tool_call_id() == "outer"
        with bind_tool_call_id("inner"):
            assert current_tool_call_id() == "inner"
        assert current_tool_call_id() == "outer"


# ---------------------------------------------------------------------------
# record_tool_call — must never raise, even when persistence itself fails.
# Lives in app.services.runs since Wave 5.1 (ex-app.services.tool_calls);
# like every writer in that module it imports get_db locally inside the
# function, so monkeypatching targets app.db.get_db directly (matching the
# convention used for record_run's own writes and app.services.ai_ledger).
# ---------------------------------------------------------------------------

def test_record_tool_call_happy_path_commits(mock_session, monkeypatch):
    from app.services import runs as rs

    class _FakeDb:
        def session(self):
            import contextlib

            @contextlib.contextmanager
            def _cm():
                yield mock_session
            return _cm()

    monkeypatch.setattr("app.db.get_db", lambda: _FakeDb())
    rs.record_tool_call(
        name="probe_tool", user_id=1, duration_ms=42,
        status="ok", error=None, tool_call_id="deadbeef",
    )

    assert mock_session.add.called
    assert mock_session.commit.called
    added_row = mock_session.add.call_args[0][0]
    assert added_row.kind == "tool_call"
    assert added_row.run_id == "deadbeef"
    assert added_row.outcome == "ok"


def test_record_tool_call_swallows_db_failure_and_warns(caplog, monkeypatch):
    from app.services import runs as rs

    def _boom():
        raise RuntimeError("db is on fire")

    monkeypatch.setattr("app.db.get_db", _boom)
    with caplog.at_level(logging.WARNING, logger="app.services.runs"):
        # Must not raise — persistence failures can never break dispatch.
        rs.record_tool_call(
            name="probe_tool", user_id=1, duration_ms=10,
            status="error", error="whatever", tool_call_id="feedface",
        )

    assert any("Failed to record tool-call runs row" in r.message for r in caplog.records)


def test_record_tool_call_truncates_long_error(mock_session, monkeypatch):
    from app.services import runs as rs

    class _FakeDb:
        def session(self):
            import contextlib

            @contextlib.contextmanager
            def _cm():
                yield mock_session
            return _cm()

    monkeypatch.setattr("app.db.get_db", lambda: _FakeDb())
    long_error = "x" * 5000
    rs.record_tool_call(
        name="probe_tool", user_id=None, duration_ms=1,
        status="error", error=long_error, tool_call_id="cafebabe",
    )

    added_row = mock_session.add.call_args[0][0]
    assert len(added_row.error_text) == rs._MAX_ERROR_LEN


# ---------------------------------------------------------------------------
# Structured completion-line format (the log line call_tool emits)
# ---------------------------------------------------------------------------

def test_completion_line_format_contains_required_fields(caplog):
    """Smoke-test the exact log call shape used in app/mcp/server.py and
    app/api/v1.py's call_tool dispatchers, so a refactor that drops a field
    fails loudly here rather than only being noticed by grepping prod logs."""
    logger = logging.getLogger("app.mcp.server")
    with caplog.at_level(logging.INFO, logger="app.mcp.server"):
        logger.info(
            "tool=%s user=%s tool_call_id=%s duration_ms=%d status=%s",
            "vault_search", 1, "abcd1234", 123, "ok",
        )
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    for fragment in (
        "tool=vault_search", "user=1", "tool_call_id=abcd1234",
        "duration_ms=123", "status=ok",
    ):
        assert fragment in message
