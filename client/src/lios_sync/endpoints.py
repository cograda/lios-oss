"""Server endpoint resolution — which configured URL is actually usable now.

The Mac moves between three network positions and the daemon has to cope with
all of them without being reconfigured:

  * home Wi-Fi        → the LAN address is fastest and doesn't traverse WireGuard
  * elsewhere + Tailscale up → only the tailnet address works
  * elsewhere + Tailscale down → nothing works; keep retrying, don't crash

`config.server.urls` has always been a list (see `ServerConfig`), but until now
only `urls[0]` was ever dialled. This module makes the list mean what it looks
like it means: an ordered preference list, probed at connect time and re-probed
whenever the connection drops.

Why probing has to be *authenticated*
-------------------------------------
The obvious implementation — "can I open a TCP connection to 192.168.1.50:8400?"
— is wrong, and wrong in a way that fails silently rather than loudly. 192.168.1.x
is the single most common home subnet, so on a foreign network that address very
often answers: a router admin page, a printer, someone else's NAS. A plain
reachability check would happily bind the client to a stranger's device and then
fail every request with a confusing 404/timeout.

So the probe is `GET /api/v1/heartbeat` with our bearer, and only HTTP 200 with a
JSON body counts. That simultaneously proves: something is listening, it speaks
comar's API, and it accepts *this user's* token. Nothing else is good enough.

Timeouts are deliberately much shorter than the request timeouts in
`server_client.py`. A probe that fails is the common case when off-LAN, and
walking a two-entry list must not cost 20 seconds of daemon startup.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

# Probe budget. Connect is the one that matters: an unreachable LAN IP either
# refuses instantly (fast) or blackholes (needs the timeout). 2s is long enough
# for home Wi-Fi and short enough that a two-URL list resolves in ~4s worst case.
PROBE_TIMEOUT = httpx.Timeout(connect=2.0, read=4.0, write=2.0, pool=2.0)

# Tailscale's CGNAT range (100.64.0.0/10) — how a raw tailnet IP is recognised.
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

# MagicDNS names all live under a tailnet's *.ts.net zone.
_TAILSCALE_SUFFIX = ".ts.net"

KIND_LAN = "lan"
KIND_TAILSCALE = "tailscale"
KIND_LOOPBACK = "loopback"
KIND_PUBLIC = "public"


@dataclass(frozen=True)
class Endpoint:
    """A candidate server URL plus what kind of path it represents."""

    url: str
    kind: str

    @property
    def is_tailscale(self) -> bool:
        return self.kind == KIND_TAILSCALE

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.url} ({self.kind})"


def normalise_url(url: str) -> str:
    """Add a scheme to bare `host:port` configs and strip the trailing slash.

    Legacy configs stored `192.168.1.50:8400` with no scheme. Default those to
    http (they are LAN-only by definition); anything reached over Tailscale is
    expected to carry an explicit https:// because Caddy fronts it with a real
    Let's Encrypt cert.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url


def classify(url: str) -> str:
    """Label a URL by the network path it implies. Display/ordering only.

    This never gates anything — an unclassifiable URL is still probed. It exists
    so logs and `lios-sync status` can say *why* the client picked what it picked,
    which is the whole point of the exercise when someone is debugging from a
    hotel.
    """
    host = urlsplit(normalise_url(url)).hostname or ""
    if host.endswith(_TAILSCALE_SUFFIX):
        return KIND_TAILSCALE
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A hostname we can't resolve to a category without DNS. Treat a
        # non-dotted name (`comar`, `dockerbox`) as LAN-ish, anything with a
        # public-looking domain as public.
        if host in ("localhost",):
            return KIND_LOOPBACK
        return KIND_LAN if "." not in host else KIND_PUBLIC
    if ip.is_loopback:
        return KIND_LOOPBACK
    if ip in _TAILSCALE_NET:
        return KIND_TAILSCALE
    if ip.is_private:
        return KIND_LAN
    return KIND_PUBLIC


def candidates(urls: Iterable[str]) -> list[Endpoint]:
    """Build the ordered candidate list, de-duplicated, preserving config order.

    Config order *is* preference order — the first entry that answers wins. We
    deliberately don't reorder by kind: someone who puts the tailnet URL first
    (e.g. a laptop that is rarely at home) has expressed a preference and the
    client should honour it rather than second-guessing with a heuristic.
    """
    seen: set[str] = set()
    out: list[Endpoint] = []
    for raw in urls:
        url = normalise_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(Endpoint(url=url, kind=classify(url)))
    return out


def _verify_arg(ca_cert: Path | str | None) -> Any:
    """httpx `verify=` value: a CA bundle path if one exists, else system trust."""
    if ca_cert:
        path = Path(ca_cert)
        if path.is_file():
            return str(path)
    return True


def probe(
    url: str,
    token: str,
    ca_cert: Path | str | None = None,
    timeout: httpx.Timeout = PROBE_TIMEOUT,
) -> bool:
    """True iff `url` is a comar server that accepts `token`.

    Authenticated on purpose — see the module docstring. A 401/403 counts as a
    failure: the endpoint may be comar, but it is not *our* comar, and moving on
    to the next candidate is more useful than binding to something that will
    reject every subsequent call.
    """
    try:
        with httpx.Client(
            base_url=normalise_url(url),
            headers={"Authorization": f"Bearer {token}"},
            verify=_verify_arg(ca_cert),
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            resp = client.get("/api/v1/heartbeat", params={"client_version": "probe"})
    except Exception as e:  # noqa: BLE001 - any transport error is just "no"
        logger.debug("Probe %s failed: %s", url, e)
        return False

    if resp.status_code != 200:
        logger.debug("Probe %s rejected: HTTP %s", url, resp.status_code)
        return False
    try:
        resp.json()
    except Exception:  # noqa: BLE001
        # Something answered 200 with non-JSON — a captive portal or a router
        # admin page on the same address. Not us.
        logger.debug("Probe %s answered 200 but not JSON — not comar", url)
        return False
    return True


def tailscale_up() -> bool:
    """Best-effort: does this machine currently have a Tailscale address?

    Used only to explain failures ("off-LAN and Tailscale is down") — never to
    decide which endpoint to use. Reading the interface list is cheap and needs
    no `tailscale` binary, no CLI parsing and no elevated permissions, unlike
    shelling out to `tailscale status`.
    """
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            try:
                if ipaddress.ip_address(addr) in _TAILSCALE_NET:
                    return True
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        pass
    # getaddrinfo on the hostname doesn't always surface the utun address; fall
    # back to asking the routing table which source address would be used to
    # reach the tailnet. No packets are sent — connect() on UDP is local-only.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("100.100.100.100", 53))  # Tailscale's MagicDNS resolver
            return ipaddress.ip_address(s.getsockname()[0]) in _TAILSCALE_NET
    except Exception:  # noqa: BLE001
        return False


def resolve(
    urls: Iterable[str],
    token: str,
    ca_cert: Path | str | None = None,
    timeout: httpx.Timeout = PROBE_TIMEOUT,
) -> Endpoint | None:
    """First candidate that answers as our server, or None if none do.

    Returning None rather than raising is deliberate: the daemon must still come
    up with no server (the vault watcher and EventKit bridge are useful offline,
    and the heartbeat loop will re-resolve every 5 minutes). Callers fall back to
    the first candidate so there is always something to retry against.
    """
    cands = list(candidates(urls))
    if not cands:
        return None
    if len(cands) == 1:
        # Nothing to choose between — skip the probe and let the real request
        # surface the real error. Probing here would only double the latency of
        # every single-URL config, which is most of them today.
        return cands[0]

    for ep in cands:
        if probe(ep.url, token, ca_cert, timeout):
            logger.info("Endpoint resolved: %s", ep)
            return ep
        logger.info("Endpoint unavailable: %s", ep)

    logger.warning(
        "No configured endpoint reachable (%s)%s",
        ", ".join(e.url for e in cands),
        "" if tailscale_up() else " — Tailscale appears to be down",
    )
    return None


def describe(
    urls: Iterable[str],
    token: str,
    ca_cert: Path | str | None = None,
) -> list[tuple[Endpoint, bool]]:
    """Probe every candidate and report each result — for `lios-sync status`.

    Unlike `resolve` this does not stop at the first success: when someone is
    diagnosing "why is it slow at home", knowing that the LAN URL is *also* down
    and it silently fell through to Tailscale is the whole answer.
    """
    return [(ep, probe(ep.url, token, ca_cert)) for ep in candidates(urls)]
