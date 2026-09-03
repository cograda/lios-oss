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
