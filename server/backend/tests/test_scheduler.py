"""Scheduler suite (db tier) — run_sync state machine on real SyncState rows.

Registers a fake integration in the real registry and drives
`scheduler.run_sync` through its outcomes: success, transient failure
(retry once), permanent failure (no retry, generic message), needs-reauth
(no retry, "needs re-auth" message), and timeout (no retry). Asserts on the
actual SyncState / SyncHistory rows the dashboard and system_alerts read.

Error classification is isinstance-based (`app.errors.TransientError` /
`PermanentError` / `NeedsReauthError`) — see `app/scheduler.py::_try_sync`.
Untyped exceptions (e.g. a bare `ConnectionError`) are treated the same as
`TransientError` (retried once); this is intentional so integrations that
haven't been migrated to the typed hierarchy yet still get sane retry
behaviour.
"""

import asyncio
import time

import pytest

from app import scheduler
from app.auth.oauth import NeedsReauthError
from app.errors import PermanentError, TransientError
from app.integrations import INTEGRATIONS
from app.models.tokens import SyncHistory, SyncState

pytestmark = pytest.mark.db


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeIntegration:
    """Minimal stand-in — run_sync only touches .sync() via the registry."""

    name = "fake_sync_probe"

    def __init__(self, behaviour):
        self.behaviour = behaviour  # callable invoked per attempt
        self.calls = 0

    def sync(self):
        self.calls += 1
        self.behaviour(self.calls)


@pytest.fixture
def register_fake(real_db, monkeypatch):
    """Register a fake integration; retry delay collapsed to zero."""
    monkeypatch.setattr(scheduler, "RETRY_DELAY_SECONDS", 0)

    created = []

    def factory(behaviour):
        integration = FakeIntegration(behaviour)
        INTEGRATIONS[integration.name] = integration
        created.append(integration.name)
        return integration

    yield factory
    for name in created:
        INTEGRATIONS.pop(name, None)


def _state(session) -> SyncState:
    return (
        session.query(SyncState)
        .filter_by(integration=FakeIntegration.name)
        .one()
    )


def _history(session) -> list[SyncHistory]:
    return (
        session.query(SyncHistory)
        .filter_by(integration=FakeIntegration.name)
        .order_by(SyncHistory.id)
        .all()
    )


@pytest.mark.anyio
async def test_success_writes_ok_state(register_fake, db_session):
    integration = register_fake(lambda n: None)

    await scheduler.run_sync(integration.name)

    state = _state(db_session)
    assert state.last_sync_status == "ok"
    assert state.consecutive_failures == 0
    assert state.last_error is None
    assert state.last_sync_duration_ms is not None
    history = _history(db_session)
    assert [h.status for h in history] == ["ok"]
    assert integration.calls == 1


@pytest.mark.anyio
async def test_transient_failure_retries_once_then_succeeds(register_fake, db_session):
    def flaky(call_number):
        if call_number == 1:
            raise ConnectionError("blip")

    integration = register_fake(flaky)
    await scheduler.run_sync(integration.name)

    assert integration.calls == 2  # initial + one retry
    assert _state(db_session).last_sync_status == "ok"


@pytest.mark.anyio
async def test_persistent_transient_failure_records_error(register_fake, db_session):
    def always_fails(call_number):
        raise ConnectionError("still down")

    integration = register_fake(always_fails)
    await scheduler.run_sync(integration.name)

    assert integration.calls == 2  # retried exactly once, no more
    state = _state(db_session)
    assert state.last_sync_status == "error"
    assert "retry failed" in state.last_error
    assert state.consecutive_failures == 1

    # A second scheduled run keeps counting.
    await scheduler.run_sync(integration.name)
    db_session.expire_all()
    assert _state(db_session).consecutive_failures == 2


@pytest.mark.anyio
async def test_needs_reauth_skips_retry(register_fake, db_session):
    def dead_token(call_number):
        raise NeedsReauthError("alex@example.com", "token revoked")

    integration = register_fake(dead_token)
    await scheduler.run_sync(integration.name)

    assert integration.calls == 1  # permanent failure → no retry
    state = _state(db_session)
    assert state.last_sync_status == "error"
    assert "needs re-auth" in state.last_error


@pytest.mark.anyio
async def test_transient_error_class_retries_once_then_succeeds(register_fake, db_session):
    """An explicit `TransientError` gets the same retry-once treatment as a
    bare Exception — classification is by isinstance, not by walking a
    wrapped exception's __cause__ chain (that belt-and-braces behaviour was
    removed once integration.sync() stopped needing asyncio.run() re-wrapping)."""

    def flaky(call_number):
        if call_number == 1:
            raise TransientError("upstream 503")

    integration = register_fake(flaky)
    await scheduler.run_sync(integration.name)

    assert integration.calls == 2  # initial + one retry
    assert _state(db_session).last_sync_status == "ok"


@pytest.mark.anyio
async def test_permanent_error_skips_retry_without_reauth_message(register_fake, db_session):
    """A generic PermanentError (not NeedsReauthError) skips retry like a
    dead token would, but must NOT get the "needs re-auth" message prefix —
    that's reserved for NeedsReauthError specifically."""

    def bad_config(call_number):
        raise PermanentError("Last.fm sync_recent: HTTP 403 (check HOME_LASTFM_API_KEY)")

    integration = register_fake(bad_config)
    await scheduler.run_sync(integration.name)

    assert integration.calls == 1  # permanent failure → no retry
    state = _state(db_session)
    assert state.last_sync_status == "error"
    assert "needs re-auth" not in state.last_error
    assert "HTTP 403" in state.last_error


@pytest.mark.anyio
async def test_timeout_records_error_without_retry(register_fake, db_session, monkeypatch):
    monkeypatch.setattr(scheduler, "SYNC_TIMEOUT_SECONDS", 0.2)

    integration = register_fake(lambda n: time.sleep(1))
    await scheduler.run_sync(integration.name)

    assert integration.calls == 1  # "timed out" errors skip the retry branch
    state = _state(db_session)
    assert state.last_sync_status == "error"
    assert "timed out" in state.last_error
