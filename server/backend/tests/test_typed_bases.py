"""Unit tests for app.plugin.bases (V4 chunk 4.1).

Covers the `SourceIntegration` template method — accounts x cursor x partial
failure — via a fake in-memory integration (no DB, no network), plus the
default-interface smoke tests for `PushSourceIntegration`, `ActionIntegration`,
and `CapabilityService`.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.errors import PermanentError, TransientError
from app.plugin.bases import (
    ActionIntegration,
    BidirectionalIntegration,
    CapabilityService,
    PullResult,
    PushSourceIntegration,
    SourceIntegration,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake SourceIntegration — accounts x cursor x partial failure
# ---------------------------------------------------------------------------


class _FakeSourceIntegration(SourceIntegration):
    """In-memory fake: `accounts()` returns plain strings, `pull()` returns a
    canned record per account (or raises, for accounts configured to fail),
    `store()` just counts. No DB/network — records/store_calls are asserted
    on directly."""

    def __init__(self, account_list: list[str], *, failing: set[str] = frozenset(),
                 cursor_key: str | None = None):
        self._accounts = account_list
        self._failing = failing
        self.store_calls: list[list[Any]] = []
        self.pull_calls: list[tuple[str, str | None]] = []
        self.cursor_key = cursor_key

    @property
    def name(self) -> str:
        return "fake_source"

    @property
    def display_name(self) -> str:
        return "Fake Source"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return []

    async def dashboard_data(self) -> dict[str, Any]:
        return {}

    def accounts(self, session: Session) -> list[str]:
        return self._accounts

    def account_user_id(self, account: str) -> int | None:
        return {"alex": 1, "sam": 2}.get(account)

    def pull(self, account: str, session: Session, cursor: str | None) -> PullResult:
        self.pull_calls.append((account, cursor))
        if account in self._failing:
            raise TransientError(f"{account} unreachable")
        return PullResult(records=[f"{account}-record"], cursor=f"{account}-cursor-1")

    def store(self, session: Session, records: list[Any]) -> int:
        self.store_calls.append(records)
        return len(records)


@pytest.fixture
def fake_db(monkeypatch):
    """Patch app.db.get_db() so SourceIntegration.sync() can open a session
    without a real Postgres connection."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    session = MagicMock()

    class _FakeDb:
        @contextmanager
        def session(self):
            yield session

    import app.db as app_db
    monkeypatch.setattr(app_db, "get_db", lambda: _FakeDb())
    return session


def test_sync_is_a_plain_function_not_a_coroutine():
    assert inspect.iscoroutinefunction(SourceIntegration.sync) is False


def test_sync_calls_pull_and_store_per_account(fake_db):
    fake = _FakeSourceIntegration(["alex", "sam"])
    fake.sync()

    assert [a for a, _ in fake.pull_calls] == ["alex", "sam"]
    assert fake.store_calls == [["alex-record"], ["sam-record"]]


def test_sync_with_no_accounts_does_not_call_pull_or_store(fake_db):
    fake = _FakeSourceIntegration([])
    fake.sync()

    assert fake.pull_calls == []
    assert fake.store_calls == []


def test_sync_partial_failure_does_not_raise_and_stores_survivors(fake_db):
    fake = _FakeSourceIntegration(["alex", "sam"], failing={"sam"})
    fake.sync()  # must not raise — partial failure is a logged, non-fatal outcome

    assert fake.store_calls == [["alex-record"]]


def test_sync_all_accounts_failing_raises(fake_db):
    fake = _FakeSourceIntegration(["alex", "sam"], failing={"alex", "sam"})
    with pytest.raises(TransientError):
        fake.sync()
    assert fake.store_calls == []


def test_sync_reads_and_writes_cursor_when_cursor_key_set(fake_db, monkeypatch):
    from app.plugin import bases as bases_module

    gets: list[tuple] = []
    sets: list[tuple] = []

    class _StubCursor:
        @staticmethod
        def get(session, integration, key, *, user_id=None):
            gets.append((integration, key, user_id))
            return "prior-cursor" if user_id == 1 else None

        @staticmethod
        def set(session, integration, key, value, *, user_id=None):
            sets.append((integration, key, value, user_id))

    monkeypatch.setattr(bases_module, "SyncCursor", _StubCursor)

    fake = _FakeSourceIntegration(["alex", "sam"], cursor_key="page")
    fake.sync()

    assert gets == [("fake_source", "page", 1), ("fake_source", "page", 2)]
    assert sets == [
        ("fake_source", "page", "alex-cursor-1", 1),
        ("fake_source", "page", "sam-cursor-1", 2),
    ]
    # The cursor read for alex ("prior-cursor") was actually threaded into pull().
    assert fake.pull_calls == [("alex", "prior-cursor"), ("sam", None)]


def test_sync_no_cursor_key_never_touches_sync_cursor(fake_db, monkeypatch):
    from app.plugin import bases as bases_module

    calls = []
    monkeypatch.setattr(
        bases_module, "SyncCursor",
        type("X", (), {
            "get": staticmethod(lambda *a, **k: calls.append(("get", a, k))),
            "set": staticmethod(lambda *a, **k: calls.append(("set", a, k))),
        }),
    )

    fake = _FakeSourceIntegration(["alex"])  # cursor_key defaults to None
    fake.sync()

    assert calls == []


# ---------------------------------------------------------------------------
# Default interfaces for the other typed bases
# ---------------------------------------------------------------------------


class _FakePushSource(PushSourceIntegration):
    @property
    def name(self) -> str:
        return "fake_push"

    @property
    def display_name(self) -> str:
        return "Fake Push"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return []

    async def dashboard_data(self) -> dict[str, Any]:
        return {}


def test_push_source_sync_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        _FakePushSource().sync()


async def _run(coro):
    return await coro


def test_push_source_probe_raises_not_implemented():
    import asyncio
    with pytest.raises(NotImplementedError):
        asyncio.run(_run(_FakePushSource().probe()))


class _FakeAction(ActionIntegration):
    @property
    def name(self) -> str:
        return "fake_action"

    @property
    def display_name(self) -> str:
        return "Fake Action"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return []


def test_action_integration_sync_and_dashboard_defaults():
    import asyncio

    fake = _FakeAction()
    assert fake.sync() is None
    assert asyncio.run(_run(fake.dashboard_data())) == {}


class _FakeCapability(CapabilityService):
    @property
    def name(self) -> str:
        return "fake_capability"

    @property
    def display_name(self) -> str:
        return "Fake Capability"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return []


def test_capability_service_sync_and_dashboard_defaults():
    import asyncio

    fake = _FakeCapability()
    assert fake.sync() is None
    assert asyncio.run(_run(fake.dashboard_data())) == {}


class _FakeBidirectional(BidirectionalIntegration):
    @property
    def name(self) -> str:
        return "fake_bidi"

    @property
    def display_name(self) -> str:
        return "Fake Bidirectional"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return []

    async def dashboard_data(self) -> dict[str, Any]:
        return {}

    def accounts(self, session):
        return []

    def pull(self, account, session, cursor):
        return PullResult(records=[])

    def store(self, session, records):
        return 0


def test_bidirectional_execute_action_raises_not_implemented():
    import asyncio

    with pytest.raises(NotImplementedError):
        asyncio.run(_run(_FakeBidirectional().execute_action({})))
