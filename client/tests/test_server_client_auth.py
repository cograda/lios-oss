"""AuthError handling in ServerClient.

401 responses must raise AuthError (with a friendly, actionable message)
rather than surfacing as a generic httpx.HTTPStatusError — the daemon and
`lios-sync status` both key off this to distinguish an expired/revoked token
from an ordinary connection failure.
"""

import httpx
import pytest

from lios_sync.config import ClientConfig, ServerConfig
from lios_sync.server_client import AuthError, ServerClient


def _config(url="http://192.168.1.50:8400", token="tok"):
    # Single URL → ServerClient._build_client()'s resolve() skips probing
    # entirely, so constructing a client never touches the network.
    return ClientConfig(user="alex", server=ServerConfig(urls=[url], token=token))


def _mock_client(monkeypatch, handler):
    """Point ServerClient's httpx.Client (used for the main + stream clients)
    at a MockTransport running `handler`, following test_endpoints.py's
    pattern."""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        __import__("lios_sync.server_client", fromlist=["httpx"]).httpx,
        "Client",
        factory,
    )


def test_heartbeat_401_raises_auth_error(monkeypatch):
    _mock_client(monkeypatch, lambda request: httpx.Response(401, json={"detail": "nope"}))
    client = ServerClient(_config())

    with pytest.raises(AuthError):
        client.heartbeat()


def test_heartbeat_success_returns_json(monkeypatch):
    _mock_client(monkeypatch, lambda request: httpx.Response(200, json={"server_time": "now"}))
    client = ServerClient(_config())

    assert client.heartbeat() == {"server_time": "now"}


def test_heartbeat_other_error_is_not_auth_error(monkeypatch):
    """A 500 is a real server error, not an auth problem — must not be
    (mis)reported as AuthError."""
    _mock_client(monkeypatch, lambda request: httpx.Response(500, text="boom"))
    client = ServerClient(_config())

    with pytest.raises(httpx.HTTPStatusError):
        client.heartbeat()


def test_get_commands_success(monkeypatch):
    payload = {"claude_md": "# hi", "commands": {"foo.md": "content"}}
    _mock_client(monkeypatch, lambda request: httpx.Response(200, json=payload))
    client = ServerClient(_config())

    assert client.get_commands() == payload


def test_get_commands_401_raises_auth_error(monkeypatch):
    _mock_client(monkeypatch, lambda request: httpx.Response(401, json={"detail": "nope"}))
    client = ServerClient(_config())

    with pytest.raises(AuthError):
        client.get_commands()


def test_auth_error_default_message_is_actionable():
    msg = str(AuthError())
    assert "expired" in msg
    assert "install code" in msg


# ---------------------------------------------------------------------------
# SSE stream liveness
# ---------------------------------------------------------------------------
#
# 2026-09-05: the daemon's events task sat 33 hours inside one `next()` on the
# SSE stream. The server container had been replaced under it, no FIN arrived,
# and the stream client had read=None — so nothing could ever raise. These two
# tests pin the two halves of the fix: a finite read timeout (the server pings
# every 15 s, so silence IS failure) and pings surfaced as items so the events
# loop's liveness beat advances on a healthy-but-quiet stream.

def _mock_client_capturing(monkeypatch, handler):
    """Like _mock_client, but also records the kwargs each httpx.Client got."""
    real_client = httpx.Client
    seen: list[dict] = []

    def factory(*args, **kwargs):
        seen.append(dict(kwargs))
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        __import__("lios_sync.server_client", fromlist=["httpx"]).httpx,
        "Client",
        factory,
    )
    return seen


_SSE_BODY = (
    b"event: hello\ndata: {\"type\": \"hello\"}\n\n"
    b": ping - 2026-09-06 11:00:00\n\n"
    b"event: eventkit_command\ndata: {\"type\": \"eventkit_command\", \"command_id\": 1}\n\n"
    b"garbage line without a field\n"
)


def test_event_stream_surfaces_pings_as_keepalive(monkeypatch):
    _mock_client_capturing(
        monkeypatch,
        lambda request: httpx.Response(200, content=_SSE_BODY,
                                       headers={"content-type": "text/event-stream"}),
    )
    client = ServerClient(_config())

    items = list(client.open_event_stream())

    assert items == [
        {"type": "hello"},
        {"type": "keepalive"},
        {"type": "eventkit_command", "command_id": 1},
    ]


def test_event_stream_client_has_finite_read_timeout(monkeypatch):
    """read=None is how a half-open socket becomes a permanent hang. The
    stream client must time out within a handful of missed 15 s pings."""
    seen = _mock_client_capturing(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"",
                                       headers={"content-type": "text/event-stream"}),
    )
    client = ServerClient(_config())
    list(client.open_event_stream())

    stream_kwargs = seen[-1]  # the stream client is built last, inside open_event_stream
    read = stream_kwargs["timeout"].read
    assert read is not None, "SSE stream client must not have an infinite read timeout"
    assert 30.0 <= read <= 120.0, read
