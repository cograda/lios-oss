"""Unit tests for the unified tool dispatcher — V4 chunk 2.1.

`app.plugin.dispatch.dispatch_tool` is the single chokepoint both the MCP
and HTTP transports now route every tool call through. These tests exercise
it directly (no ASGI, no real Postgres) — the DB-backed, transport-level
coverage (auth paths, real freshness/sync, real `tool_calls` persistence)
stays in tests/test_mcp_transport.py and tests/test_tool_calls.py (db tier).
"""

import contextlib
import time

import pytest

from app.models.users import User


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _user(user_id: int, name: str = "alex") -> User:
    return User(id=user_id, name=name, display_name=name.title())


@pytest.fixture
def fake_db(mock_session):
    """A minimal `get_db()`-shaped stand-in around the shared mock_session."""

    class _FakeDb:
        def session(self):
            @contextlib.contextmanager
            def _cm():
                yield mock_session

            return _cm()

    return _FakeDb()


@pytest.fixture(autouse=True)
def _patch_get_db(monkeypatch, fake_db):
    import app.plugin.dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "get_db", lambda: fake_db)


@pytest.fixture
def recorded_calls(monkeypatch):
    """Capture record_tool_call(...) kwargs instead of hitting a real DB."""
    calls: list[dict] = []

    def _fake_record(**kwargs):
        calls.append(kwargs)

    import app.plugin.dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "record_tool_call", _fake_record)
    return calls


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Give each test its own tool_handlers dict so probes don't leak."""
    from app.plugin import registry

    monkeypatch.setattr(registry, "tool_handlers", dict(registry.tool_handlers))
    yield registry.tool_handlers


def _register(registry_dict, name, handler, integration="probe"):
    registry_dict[name] = (handler, integration)


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_unknown_tool_returns_typed_error_without_recording(recorded_calls):
    from app.plugin.dispatch import dispatch_tool

    result = await dispatch_tool("does_not_exist", {}, _user(1))

    assert result.is_error is True
    assert "Unknown tool" in result.content
    assert result.tool_call_id is None
    # Matches pre-chunk behavior: unknown-tool short-circuits before any
    # tool_calls bookkeeping runs (both transports returned early here).
    assert recorded_calls == []


# ---------------------------------------------------------------------------
# Handler exception
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_handler_exception_is_error_and_records_row(_isolated_registry, recorded_calls):
    from app.plugin.dispatch import dispatch_tool

    def exploding(session, arguments):
        raise ValueError("kaboom")

    _register(_isolated_registry, "probe_explode", exploding)

    result = await dispatch_tool("probe_explode", {}, _user(1))

    assert result.is_error is True
    assert result.status == "error"
    assert "kaboom" in result.content
    assert result.tool_call_id is not None

    assert len(recorded_calls) == 1
    row = recorded_calls[0]
    assert row["name"] == "probe_explode"
    assert row["user_id"] == 1
    assert row["status"] == "error"
    assert "kaboom" in row["error"]
    assert row["tool_call_id"] == result.tool_call_id


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_timeout_path(_isolated_registry, recorded_calls):
    from app.plugin.dispatch import dispatch_tool

    def slow(session, arguments):
        time.sleep(1)
        return "{}"

    _register(_isolated_registry, "probe_slow", slow)

    result = await dispatch_tool("probe_slow", {}, _user(1), timeout=0.05)

    assert result.is_error is True
    assert result.status == "timeout"
    assert "timed out" in result.content

    assert len(recorded_calls) == 1
    assert recorded_calls[0]["status"] == "timeout"


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_success_path_records_ok_row(_isolated_registry, recorded_calls):
    from app.plugin.dispatch import dispatch_tool

    def handler(session, arguments):
        return {"hello": "world"}

    _register(_isolated_registry, "probe_ok", handler)

    result = await dispatch_tool("probe_ok", {}, _user(1))

    assert result.is_error is False
    assert result.status == "ok"
    assert '"hello": "world"' in result.content

    assert len(recorded_calls) == 1
    assert recorded_calls[0]["status"] == "ok"
    assert recorded_calls[0]["error"] is None


# ---------------------------------------------------------------------------
# ContextVar isolation — no bleed across calls/users
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_contextvar_reset_no_bleed_across_users(_isolated_registry, recorded_calls):
    from app.auth.context import current_user_id
    from app.plugin.dispatch import current_tool_call_id, dispatch_tool

    seen_user_ids = []

    def whoami(session, arguments):
        seen_user_ids.append(current_user_id())
        return {"user_id": current_user_id()}

    _register(_isolated_registry, "probe_whoami", whoami)

    assert current_tool_call_id() is None

    result_1 = await dispatch_tool("probe_whoami", {}, _user(1))
    assert current_tool_call_id() is None  # reset after dispatch
    call_id_1 = result_1.tool_call_id

    result_2 = await dispatch_tool("probe_whoami", {}, _user(2))
    assert current_tool_call_id() is None  # reset after dispatch
    call_id_2 = result_2.tool_call_id

    # Each call saw its own user, no bleed from the previous call.
    assert seen_user_ids == [1, 2]
    # Each call got its own correlation id.
    assert call_id_1 != call_id_2
    assert recorded_calls[0]["user_id"] == 1
    assert recorded_calls[1]["user_id"] == 2
