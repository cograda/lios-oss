"""Tests for D.5 step 1 — server-side EventKit command dispatch.

Avoids importing the integration package (which transitively pulls fastapi)
by loading commands.py directly via spec_from_file_location, mirroring the
pattern used by test_apple_reminders_tools.py.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _ensure_pkg(name):
    if name in sys.modules:
        return sys.modules[name]
    try:
        import importlib
        return importlib.import_module(name)
    except Exception:
        m = types.ModuleType(name)
        m.__path__ = []
        sys.modules[name] = m
        return m


# Stub ReminderCommand model so commands.py can import it without SQLAlchemy.
class _StubReminderCommand:
    def __init__(self, **kwargs):
        self.id = None
        self.user_id = kwargs.get("user_id")
        self.action = kwargs.get("action")
        self.payload = kwargs.get("payload")
        self.status = kwargs.get("status", "pending")
        self.processed_at = None


_stub_models = types.ModuleType("app.integrations.apple_reminders.models")
_stub_models.ReminderCommand = _StubReminderCommand


# Real stream_manager — it's pure asyncio + dataclasses, no fastapi.
_ensure_pkg("app")
_ensure_pkg("app.integrations")
_ensure_pkg("app.integrations.apple_reminders")
sys.modules["app.integrations.apple_reminders.models"] = _stub_models

import importlib  # noqa: E402

stream_manager_mod = importlib.import_module("app.stream_manager")

# Now load commands.py directly.
_commands_path = (
    Path(__file__).resolve().parent.parent
    / "app" / "integrations" / "apple_reminders" / "commands.py"
)
_spec = importlib.util.spec_from_file_location(
    "app.integrations.apple_reminders.commands", str(_commands_path),
)
commands_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commands_mod)


@pytest.fixture(autouse=True)
def _clear_pending():
    commands_mod._pending.clear()
    yield
    commands_mod._pending.clear()


def _fake_session_with_command(command_id: int = 1):
    session = MagicMock()

    def _refresh(obj):
        obj.id = command_id
    session.refresh.side_effect = _refresh
    return session


def test_dispatch_returns_queued_when_no_loop_bound(monkeypatch):
    monkeypatch.setattr(commands_mod.stream_manager, "loop", None)
    session = _fake_session_with_command(42)

    result = commands_mod.dispatch_command(
        session, user_id=1, user_name="alex",
        action="add", args={"summary": "test"},
    )

    assert result["ok"] is True
    assert result["queued"] is True
    assert result["command_id"] == 42
    assert "loop" in result["reason"]


def test_dispatch_returns_queued_when_no_subscriber():
    """Bound loop with zero subscribers → publish returns 0 → queued."""
    loop = asyncio.new_event_loop()
    fresh_sm = stream_manager_mod.StreamManager()
    fresh_sm.loop = loop

    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        original = commands_mod.stream_manager
        commands_mod.stream_manager = fresh_sm
        try:
            session = _fake_session_with_command(7)
            result = commands_mod.dispatch_command(
                session, user_id=1, user_name="alex",
                action="add", args={"summary": "no subs"},
                timeout=0.1,
            )
        finally:
            commands_mod.stream_manager = original
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=1)
        loop.close()

    assert result["queued"] is True
    assert result["command_id"] == 7
    assert "no client" in result["reason"]


def test_complete_command_resolves_pending_future():
    import concurrent.futures
    fut = concurrent.futures.Future()
    commands_mod._pending[99] = fut

    cmd = _StubReminderCommand(user_id=1)
    cmd.id = 99
    cmd.payload = json.dumps({"args": {"summary": "x"}})
    session = MagicMock()
    session.get.return_value = cmd

    ok = commands_mod.complete_command(
        session, command_id=99, user_id=1, result={"uid": "abc"},
    )

    assert ok is True
    assert cmd.status == "done"
    persisted = json.loads(cmd.payload)
    assert persisted["result"] == {"uid": "abc"}
    assert fut.done()
    assert fut.result(timeout=0) == {"uid": "abc"}


def test_complete_command_rejects_other_users_command():
    cmd = _StubReminderCommand(user_id=1)
    cmd.id = 5
    cmd.payload = json.dumps({"args": {}})
    session = MagicMock()
    session.get.return_value = cmd

    ok = commands_mod.complete_command(
        session, command_id=5, user_id=2, result={"uid": "x"},
    )

    assert ok is False
    assert cmd.status != "done"


def test_complete_command_with_error_sets_failed():
    import concurrent.futures
    fut = concurrent.futures.Future()
    commands_mod._pending[3] = fut

    cmd = _StubReminderCommand(user_id=1)
    cmd.id = 3
    cmd.payload = json.dumps({"args": {}})
    session = MagicMock()
    session.get.return_value = cmd

    ok = commands_mod.complete_command(
        session, command_id=3, user_id=1, error="EventKit failure",
    )

    assert ok is True
    assert cmd.status == "failed"
    persisted = json.loads(cmd.payload)
    assert persisted["error"] == "EventKit failure"
    with pytest.raises(RuntimeError, match="EventKit failure"):
        fut.result(timeout=0)
