"""Unit tests for the unified tool dispatcher — V4 chunk 2.1.

`app.plugin.dispatch.dispatch_tool` is the single chokepoint both the MCP
and HTTP transports now route every tool call through. These tests exercise
it directly (no ASGI, no real Postgres) — the DB-backed, transport-level
coverage (auth paths, real freshness/sync, real tool-call `runs` persistence)
stays in tests/test_mcp_transport.py and tests/test_tool_call_runs.py (db tier).
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
    # runs (kind="tool_call") bookkeeping (both transports returned early here).
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


# ---------------------------------------------------------------------------
# Grant resolution binds the user AND the folder scope, together
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_grant_folders_are_bound_with_the_user_and_reset_after(
    _isolated_registry, recorded_calls, monkeypatch,
):
    """A folder-scoped grant must reach the handler as `current_vault_folders()`.

    A user swap without its folders is a whole-vault read the grant did not
    give, so the two are bound in one `with` — this pins that, and that
    neither binding outlives the call.
    """
    import app.plugin.dispatch as dispatch_mod
    from app.auth.context import current_user_id, current_vault_folders
    from app.plugin.dispatch import dispatch_tool
    from app.services.vault_grants import GrantResolution

    monkeypatch.setattr(
        dispatch_mod, "resolve_grant",
        lambda *a, **k: GrantResolution(2, ("Household/", "People/")),
    )
    seen = []

    def probe(session, arguments):
        seen.append((current_user_id(), current_vault_folders()))
        return {}

    _register(_isolated_registry, "probe_scope", probe)

    result = await dispatch_tool("probe_scope", {"as_user": "sam"}, _user(1))
    assert result.is_error is False
    assert seen == [(2, ("Household/", "People/"))]
    assert current_vault_folders() is None  # reset after dispatch

    # A whole-vault resolution binds no folder scope.
    monkeypatch.setattr(dispatch_mod, "resolve_grant", lambda *a, **k: GrantResolution(2))
    await dispatch_tool("probe_scope", {"as_user": "sam"}, _user(1))
    assert seen[-1] == (2, None)


# ---------------------------------------------------------------------------
# Token scope (2026-09-07) — a `readonly` bearer may call only tools that
# positively declare readOnlyHint: true. No annotations = a write.
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_metadata(monkeypatch):
    """Own `tool_metadata` dict per test — dispatch reads it via
    `registry.get_tool_annotations`, so the module attribute is what matters."""
    from app.plugin import registry

    monkeypatch.setattr(registry, "tool_metadata", dict(registry.tool_metadata))
    yield registry.tool_metadata


def _scoped_user(scope: str) -> User:
    u = _user(3, "agent")
    u.client_token_id = 42
    u.client_token_scope = scope
    return u


def _register_probe_tools(handlers, metadata):
    def ok(session, arguments):
        return {"ran": True}

    _register(handlers, "probe_read", ok)
    metadata["probe_read"] = {"annotations": {"readOnlyHint": True, "destructiveHint": False}}
    _register(handlers, "probe_write", ok)
    metadata["probe_write"] = {"annotations": {"readOnlyHint": False, "destructiveHint": False}}
    _register(handlers, "probe_unannotated", ok)
    metadata.pop("probe_unannotated", None)


@pytest.mark.anyio
async def test_readonly_token_may_call_a_read_only_tool(
    _isolated_registry, _isolated_metadata, recorded_calls,
):
    from app.plugin.dispatch import dispatch_tool

    _register_probe_tools(_isolated_registry, _isolated_metadata)
    result = await dispatch_tool("probe_read", {}, _scoped_user("readonly"))

    assert result.is_error is False
    assert '"ran": true' in result.content
    assert len(recorded_calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["probe_write", "probe_unannotated"])
async def test_readonly_token_is_refused_a_write_or_unannotated_tool(
    _isolated_registry, _isolated_metadata, recorded_calls, tool,
):
    from app.plugin.dispatch import dispatch_tool

    ran = []

    def spy(session, arguments):
        ran.append(True)
        return {}

    _register_probe_tools(_isolated_registry, _isolated_metadata)
    _register(_isolated_registry, tool, spy)

    result = await dispatch_tool(tool, {}, _scoped_user("readonly"))

    assert result.is_error is True and result.status == "error"
    assert tool in result.content and "read-only token" in result.content
    assert ran == [], "the handler must never run for a refused call"
    # Refused before bookkeeping, like an unknown tool.
    assert recorded_calls == []


@pytest.mark.anyio
async def test_full_token_calls_write_and_unannotated_tools_as_before(
    _isolated_registry, _isolated_metadata, recorded_calls,
):
    from app.plugin.dispatch import dispatch_tool

    _register_probe_tools(_isolated_registry, _isolated_metadata)
    for tool in ("probe_write", "probe_unannotated", "probe_read"):
        result = await dispatch_tool(tool, {}, _scoped_user("full"))
        assert result.is_error is False, tool
    # And a user with no scope attribute at all (OAuth sessions) is `full`.
    result = await dispatch_tool("probe_write", {}, _user(1))
    assert result.is_error is False
