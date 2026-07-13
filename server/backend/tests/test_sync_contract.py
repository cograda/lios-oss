"""Sync contract meta-test (unit tier).

Phase 2a of the architecture plan made the model honest: every integration's
sync() performs only blocking I/O (sync SQLAlchemy + sync httpx), so the
scheduler runs it via asyncio.to_thread rather than pretending it's a
coroutine and bridging with asyncio.run(). This locks that in — both for the
ABC and for every concrete integration in the live registry.
"""

import inspect

from app.integrations import INTEGRATIONS, register_all
from app.integrations.base import BaseIntegration


def test_base_integration_sync_is_not_a_coroutine_function():
    assert inspect.iscoroutinefunction(BaseIntegration.sync) is False


def test_no_integration_declares_async_sync():
    INTEGRATIONS.clear()
    register_all()

    assert INTEGRATIONS, "expected register_all() to populate the registry"

    coroutine_syncs = [
        name
        for name, integration in INTEGRATIONS.items()
        if inspect.iscoroutinefunction(integration.sync)
    ]
    assert coroutine_syncs == []
