"""Endpoint resolution — classification, probing, and preference ordering."""

import httpx
import pytest

from lios_sync import endpoints
from lios_sync.endpoints import Endpoint, candidates, classify, normalise_url, probe, resolve


# -- normalise / classify ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("192.168.1.50:8400", "http://192.168.1.50:8400"),
        ("http://192.168.1.50:8400/", "http://192.168.1.50:8400"),
        ("https://box.tail1234.ts.net", "https://box.tail1234.ts.net"),
        ("  http://a.b/  ", "http://a.b"),
        ("", ""),
    ],
)
def test_normalise_url(raw, expected):
    assert normalise_url(raw) == expected


@pytest.mark.parametrize(
    "url,kind",
    [
        ("http://192.168.1.50:8400", endpoints.KIND_LAN),
        ("http://10.0.0.4:8400", endpoints.KIND_LAN),
        ("https://box.tail78010b.ts.net", endpoints.KIND_TAILSCALE),
        # A raw tailnet IP is in the CGNAT range, not RFC1918 — must not be
        # mistaken for LAN, or `lios-sync status` would lie about the path in use.
        ("http://100.82.221.91:8400", endpoints.KIND_TAILSCALE),
        ("http://localhost:9443", endpoints.KIND_LOOPBACK),
        ("http://127.0.0.1:9443", endpoints.KIND_LOOPBACK),
        ("https://comar.example.com", endpoints.KIND_PUBLIC),
    ],
)
def test_classify(url, kind):
    assert classify(url) == kind


def test_candidates_dedupes_and_preserves_order():
    got = candidates([
        "192.168.1.50:8400",
        "http://192.168.1.50:8400/",   # same thing after normalisation
        "https://box.tail1.ts.net",
    ])
    assert [e.url for e in got] == [
        "http://192.168.1.50:8400",
        "https://box.tail1.ts.net",
    ]
    assert got[1].is_tailscale


# -- probe -------------------------------------------------------------------


def _client_factory(monkeypatch, handler):
    """Point endpoints' httpx.Client at a MockTransport running `handler`."""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(endpoints.httpx, "Client", factory)


def test_probe_accepts_authenticated_json_200(monkeypatch):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"server_time": "now"})

    _client_factory(monkeypatch, handler)
    assert probe("http://192.168.1.50:8400", "tok") is True


def test_probe_rejects_401(monkeypatch):
    """A server that doesn't accept our token is not our server."""
    _client_factory(monkeypatch, lambda request: httpx.Response(401, json={"detail": "nope"}))
    assert probe("http://192.168.1.50:8400", "tok") is False


def test_probe_rejects_200_html(monkeypatch):
    """The failure mode this whole module exists for.

    On a foreign 192.168.1.x network some *other* device answers on the
    configured LAN address. A plain reachability check would bind to it; the
    JSON check refuses.
    """
    _client_factory(monkeypatch, lambda request: httpx.Response(200, text="<html>Router Login</html>"))
    assert probe("http://192.168.1.50:8400", "tok") is False


def test_probe_rejects_transport_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    _client_factory(monkeypatch, handler)
    assert probe("http://192.168.1.50:8400", "tok") is False


# -- resolve -----------------------------------------------------------------


def test_resolve_single_url_skips_probing(monkeypatch):
    """One candidate means no choice to make — don't pay for a probe."""
    called = []
    monkeypatch.setattr(endpoints, "probe", lambda *a, **k: called.append(a) or True)

    got = resolve(["http://192.168.1.50:8400"], "tok")
    assert got == Endpoint("http://192.168.1.50:8400", endpoints.KIND_LAN)
    assert called == []


def test_resolve_prefers_first_reachable(monkeypatch):
    monkeypatch.setattr(endpoints, "probe", lambda url, *a, **k: True)

    got = resolve(["http://192.168.1.50:8400", "https://box.tail1.ts.net"], "tok")
    assert got.url == "http://192.168.1.50:8400"


def test_resolve_falls_through_to_tailscale(monkeypatch):
    monkeypatch.setattr(
        endpoints, "probe", lambda url, *a, **k: "ts.net" in url,
    )

    got = resolve(["http://192.168.1.50:8400", "https://box.tail1.ts.net"], "tok")
    assert got.url == "https://box.tail1.ts.net"
    assert got.is_tailscale


def test_resolve_returns_none_when_nothing_answers(monkeypatch):
    """Off-network with Tailscale down. The daemon must still start."""
    monkeypatch.setattr(endpoints, "probe", lambda *a, **k: False)
    monkeypatch.setattr(endpoints, "tailscale_up", lambda: False)

    assert resolve(["http://192.168.1.50:8400", "https://box.tail1.ts.net"], "tok") is None


def test_resolve_empty_list():
    assert resolve([], "tok") is None
