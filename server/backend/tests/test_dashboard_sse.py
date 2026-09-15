"""SSE-driven dashboard sync state (issue #141) — unit tier.

The dashboard's daemon/sync-state view (`ClientStatusCard`,
`SystemAlertsPanel`) used to poll `/api/auth/clients` (60s) and
`/api/system/alerts` (30s) on a timer. `GET /api/v1/events` already gave the
daemon a push channel, but it is bearer-authenticated — a credential that
must never reach the browser (see `server/CLAUDE.md`'s auth section) — so
it cannot be the dashboard's own stream.

This adds a second, session-gated stream (`GET /api/system/events`,
`routes/system.py::system_events`) riding the same `stream_manager` pub/sub
hub on its own channel (`DASHBOARD_CHANNEL`/`DASHBOARD_CHANNEL_USER`, never
`"sse"` — that channel is the per-user bearer stream a daemon owns, and a
daemon receiving an event shape it doesn't expect is not the failure mode
to introduce). `api/v1.py`'s `events_stream` (daemon connect/disconnect)
and `heartbeat` (daemon heartbeat) publish onto it.

Covers:
  (a) the pub/sub roundtrip on the dashboard's own channel key
  (b) `events_stream` publishes `daemon_connection` connect=True on
      subscribe and connect=False on disconnect
  (c) `heartbeat` publishes `daemon_heartbeat` when it writes
      client_version/task_health, via the thread-hop helper
      `dispatch_command` already established a pattern for
  (d) `routes.system.system_events`'s generator: sends `hello` first, then
      forwards a published event under its own `type` as the SSE `event`
      name, and unsubscribes from the dashboard channel on close
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_user(name: str = "alex", user_id: int = 1):
    user = MagicMock()
    user.name = name
    user.id = user_id
    return user


# ---------------------------------------------------------------------------
# (a) stream_manager pub/sub on the dashboard channel
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_dashboard_channel_roundtrip():
    from app.stream_manager import DASHBOARD_CHANNEL, DASHBOARD_CHANNEL_USER, StreamManager

    sm = StreamManager()
    queue = await sm.subscribe(DASHBOARD_CHANNEL_USER, channel=DASHBOARD_CHANNEL)

    delivered = await sm.publish(
        {"type": "daemon_connection", "user": "alex", "connected": True},
        target_user=DASHBOARD_CHANNEL_USER,
    )
    assert delivered == 1
    event = queue.get_nowait()
    assert event == {"type": "daemon_connection", "user": "alex", "connected": True}

    await sm.unsubscribe(DASHBOARD_CHANNEL_USER, channel=DASHBOARD_CHANNEL, queue=queue)
    assert sm.subscriber_count == 0


@pytest.mark.anyio
async def test_dashboard_channel_does_not_leak_to_bearer_sse_subscribers():
    """A daemon subscribed on `channel="sse"` (its own username) must never
    receive a dashboard-targeted publish — `publish(target_user=...)` is
    already scoped by user, and `DASHBOARD_CHANNEL_USER` is not a real
    username, so this is really pinning that the two channels don't share
    a key by accident."""
    from app.stream_manager import DASHBOARD_CHANNEL, DASHBOARD_CHANNEL_USER, StreamManager

    sm = StreamManager()
    daemon_queue = await sm.subscribe("alex", channel="sse")
    dash_queue = await sm.subscribe(DASHBOARD_CHANNEL_USER, channel=DASHBOARD_CHANNEL)

    await sm.publish({"type": "daemon_heartbeat"}, target_user=DASHBOARD_CHANNEL_USER)

    assert dash_queue.qsize() == 1
    assert daemon_queue.qsize() == 0


# ---------------------------------------------------------------------------
# (b) events_stream (api/v1.py) publishes daemon_connection on connect/disconnect
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_events_stream_announces_connect_and_disconnect(monkeypatch):
    from app.api import v1 as v1_mod
    from app.stream_manager import DASHBOARD_CHANNEL, DASHBOARD_CHANNEL_USER, StreamManager

    fresh_sm = StreamManager()
    monkeypatch.setattr(v1_mod, "stream_manager", fresh_sm)

    # events_stream drains pending reminder commands via a capability lookup
    # that isn't wired up in this unit test — make it a clean no-op rather
    # than pulling in the reminders integration.
    monkeypatch.setattr(
        "app.plugin.capabilities.get_capability",
        lambda name: MagicMock(drain_pending_commands=lambda **kw: {}),
    )

    dash_queue = await fresh_sm.subscribe(DASHBOARD_CHANNEL_USER, channel=DASHBOARD_CHANNEL)

    user = _make_user("alex")
    response = await v1_mod.events_stream(user=user)

    # Drive the generator far enough to pass the connect announcement + the
    # "hello" yield (subscribe happens before the generator is even
    # created, so the dashboard event is already queued by this point).
    hello = await response.body_iterator.__anext__()
    assert hello["event"] == "hello"

    connect_event = dash_queue.get_nowait()
    assert connect_event == {"type": "daemon_connection", "user": "alex", "connected": True}

    # Closing the generator runs its `finally` — unsubscribe + disconnect announce.
    await response.body_iterator.aclose()

    disconnect_event = dash_queue.get_nowait()
    assert disconnect_event == {"type": "daemon_connection", "user": "alex", "connected": False}


@pytest.mark.anyio
async def test_events_stream_disconnect_silent_while_another_subscriber_remains(monkeypatch):
    """Two daemons (or two tabs) for the same user: closing one must not
    announce "gone" while the other is still subscribed — multi-subscriber-
    per-key is the whole reason `stream_manager` fans out rather than
    evicting (see its own module docstring)."""
    from app.api import v1 as v1_mod
    from app.stream_manager import DASHBOARD_CHANNEL, DASHBOARD_CHANNEL_USER, StreamManager

    fresh_sm = StreamManager()
    monkeypatch.setattr(v1_mod, "stream_manager", fresh_sm)
    monkeypatch.setattr(
        "app.plugin.capabilities.get_capability",
        lambda name: MagicMock(drain_pending_commands=lambda **kw: {}),
    )

    dash_queue = await fresh_sm.subscribe(DASHBOARD_CHANNEL_USER, channel=DASHBOARD_CHANNEL)

    user = _make_user("alex")
    first = await v1_mod.events_stream(user=user)
    await first.body_iterator.__anext__()  # hello
    dash_queue.get_nowait()  # connect announce #1

    second = await v1_mod.events_stream(user=user)
    await second.body_iterator.__anext__()  # hello
    dash_queue.get_nowait()  # connect announce #2

    await first.body_iterator.aclose()
    assert dash_queue.empty(), "closing one of two subscribers must not announce disconnect"

    await second.body_iterator.aclose()
    disconnect_event = dash_queue.get_nowait()
    assert disconnect_event["connected"] is False


# ---------------------------------------------------------------------------
# (c) heartbeat publishes daemon_heartbeat via the thread-hop helper
# ---------------------------------------------------------------------------

def test_notify_dashboard_from_thread_is_a_noop_with_no_bound_loop(monkeypatch):
    """No event loop bound (server hasn't finished startup) — degrades to
    nothing rather than raising into a request handler."""
    from app.api import v1 as v1_mod

    monkeypatch.setattr(v1_mod.stream_manager, "loop", None)
    v1_mod._notify_dashboard_from_thread("daemon_heartbeat", user="alex")  # must not raise


def test_notify_dashboard_from_thread_publishes_onto_dashboard_channel(monkeypatch):
    import threading

    from app.api import v1 as v1_mod
    from app.stream_manager import DASHBOARD_CHANNEL, DASHBOARD_CHANNEL_USER, StreamManager

    loop = asyncio.new_event_loop()
    fresh_sm = StreamManager()
    fresh_sm.loop = loop
    monkeypatch.setattr(v1_mod, "stream_manager", fresh_sm)

    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        queue = asyncio.run_coroutine_threadsafe(
            fresh_sm.subscribe(DASHBOARD_CHANNEL_USER, channel=DASHBOARD_CHANNEL), loop,
        ).result(timeout=1)

        v1_mod._notify_dashboard_from_thread("daemon_heartbeat", user="alex")

        async def _get():
            return await asyncio.wait_for(queue.get(), timeout=1)

        event = asyncio.run_coroutine_threadsafe(_get(), loop).result(timeout=2)
        assert event == {"type": "daemon_heartbeat", "user": "alex"}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=1)
        loop.close()


# ---------------------------------------------------------------------------
# (d) routes/system.py's system_events generator
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_system_events_hello_then_forwards_published_event(monkeypatch):
    from app.routes import system as system_routes
    from app.stream_manager import DASHBOARD_CHANNEL, DASHBOARD_CHANNEL_USER, stream_manager

    response = await system_routes.system_events()
    hello = await response.body_iterator.__anext__()
    assert hello["event"] == "hello"
    assert json.loads(hello["data"]) == {"ok": True}

    delivered = await stream_manager.publish(
        {"type": "daemon_heartbeat", "user": "sam"}, target_user=DASHBOARD_CHANNEL_USER,
    )
    assert delivered == 1

    forwarded = await response.body_iterator.__anext__()
    assert forwarded["event"] == "daemon_heartbeat"
    assert json.loads(forwarded["data"]) == {"type": "daemon_heartbeat", "user": "sam"}

    subscribers_before = stream_manager.subscriber_count
    await response.body_iterator.aclose()
    # aclose() drives the generator's `finally`, which unsubscribes.
    assert stream_manager.subscriber_count == subscribers_before - 1
