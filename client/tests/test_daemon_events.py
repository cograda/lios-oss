"""Tests for the daemon's SSE event handling.

Drives `_handle_server_event` / `_handle_eventkit_command` with stub
stores and a stub ServerClient — no network, no EventKit. Async entry
points run via asyncio.run() so no pytest async plugin is needed.
"""

import asyncio

from lios_sync.daemon import (
    _handle_eventkit_command,
    _handle_server_event,
    _next_event,
)


class StubServerClient:
    def __init__(self, prompts=None, ack_raises=False):
        self.prompts = prompts or []
        self.acks = []
        self.ack_raises = ack_raises

    def list_prompts(self):
        return self.prompts

    def ack_reminder_command(self, command_id, result=None, error=None):
        if self.ack_raises:
            raise ConnectionError("server gone")
        self.acks.append({"command_id": command_id, "result": result, "error": error})


class StubReminderStore:
    is_available = True

    def __init__(self, complete_ok=True):
        self.added = []
        self.completed = []
        self.complete_ok = complete_ok

    def add_reminder(self, summary, list_name, due_date, priority, notes, account_email):
        self.added.append(summary)
        return "uid-123"

    def complete_reminder(self, uid):
        self.completed.append(uid)
        return self.complete_ok


def _run(event, server_client=None, reminder_store=None):
    asyncio.run(_handle_server_event(
        event,
        server_client or StubServerClient(),
        reminder_store,
    ))


# ---------------------------------------------------------------------------
# Stream plumbing
# ---------------------------------------------------------------------------

def test_next_event_returns_none_on_exhausted_stream():
    assert _next_event(iter([])) is None


def test_next_event_pulls_in_order():
    stream = iter([{"type": "hello"}, {"type": "x"}])
    assert _next_event(stream) == {"type": "hello"}
    assert _next_event(stream) == {"type": "x"}


# ---------------------------------------------------------------------------
# prompt_update / hello / unknown
# ---------------------------------------------------------------------------

def test_prompt_update_is_noop():
    """Phase 4 (2026-07-14): prompt mirroring retired along with the local
    MCP app. The server may still emit prompt_update events; the daemon
    just logs and ignores them."""
    client = StubServerClient()
    _run({"type": "prompt_update", "prompt_set_hash": "new"}, client)
    assert client.acks == []


def test_hello_and_unknown_events_are_noops():
    _run({"type": "hello"})
    _run({"type": "mystery_event"})
    _run({})  # no type at all


# ---------------------------------------------------------------------------
# eventkit_command
# ---------------------------------------------------------------------------

def test_add_command_executes_and_acks_result():
    client = StubServerClient()
    store = StubReminderStore()

    _run(
        {"type": "eventkit_command", "command_id": 7, "action": "add",
         "args": {"summary": "Buy milk", "list": "Groceries"}},
        server_client=client, reminder_store=store,
    )

    assert store.added == ["Buy milk"]
    assert client.acks == [{"command_id": 7, "result": {"uid": "uid-123"}, "error": None}]


def test_complete_command_acks_success():
    client = StubServerClient()
    store = StubReminderStore()

    _run(
        {"type": "eventkit_command", "command_id": 8, "action": "complete",
         "args": {"uid": "uid-123"}},
        server_client=client, reminder_store=store,
    )

    assert store.completed == ["uid-123"]
    assert client.acks[0]["result"] == {"uid": "uid-123", "completed": True}


def test_complete_command_not_found_acks_error():
    client = StubServerClient()
    store = StubReminderStore(complete_ok=False)

    _run(
        {"type": "eventkit_command", "command_id": 9, "action": "complete",
         "args": {"uid": "ghost"}},
        server_client=client, reminder_store=store,
    )

    assert client.acks[0]["result"] is None
    assert "not found" in client.acks[0]["error"]


def test_unknown_action_acks_error():
    client = StubServerClient()

    _run(
        {"type": "eventkit_command", "command_id": 10, "action": "teleport", "args": {}},
        server_client=client, reminder_store=StubReminderStore(),
    )

    assert "Unknown action" in client.acks[0]["error"]


def test_unavailable_eventkit_acks_error_without_executing():
    client = StubServerClient()

    _run(
        {"type": "eventkit_command", "command_id": 11, "action": "add", "args": {}},
        server_client=client, reminder_store=None,
    )

    assert "not available" in client.acks[0]["error"]


def test_malformed_command_is_dropped_without_ack():
    client = StubServerClient()

    _run({"type": "eventkit_command", "args": {}}, server_client=client,
         reminder_store=StubReminderStore())

    assert client.acks == []


def test_ack_failure_does_not_propagate():
    client = StubServerClient(ack_raises=True)
    store = StubReminderStore()

    # Must not raise even though every ack attempt explodes.
    _run(
        {"type": "eventkit_command", "command_id": 12, "action": "add",
         "args": {"summary": "x"}},
        server_client=client, reminder_store=store,
    )

    assert store.added == ["x"]
