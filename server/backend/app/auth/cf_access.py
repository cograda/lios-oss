"""Cloudflare Access JWT verification (lios#139).

Edge-side defence in depth for `POST /api/inbox/ingest`, the one route
reachable through the Cloudflare tunnel (`deploy/cloudflared/config.yml`;
see `deploy/docs/cloudflare-tunnel.md`). Everything else on that hostname
404s at cloudflared itself and never reaches this process.

## History — read before touching this again

A `CF-Access-Client-Id` allow-list shipped and was reverted the same
evening (PR #64 / #65): Cloudflare Access authenticates the *service
token* at the edge but does **not** forward its client id to the origin.
Every real capture through the tunnel got a 403 with the check enabled.

What Access *does* forward is a signed JWT in the `Cf-Access-Jwt-Assertion`
header. That is what this module verifies:

  - signature against the team's JWKS (`https://<team>.cloudflareaccess
    .com/cdn-cgi/access/certs`), cached, refetched on an unrecognised `kid`
  - `iss` == `https://<team>.cloudflareaccess.com`
  - `aud` contains the configured Access application's AUD tag
  - `exp` / `nbf` respected (PyJWT enforces both from the claims present)

## Configuration

Two settings (`app/config.py`): `HOME_CF_ACCESS_TEAM_DOMAIN` and
`HOME_CF_ACCESS_AUD`. Both unset (the default — every existing LAN/tailnet
deploy, and the whole test suite) is a no-op: `is_configured()` is False,
`enforce()` returns immediately, and `log_startup_status()` logs once that
the check is disabled. Setting both turns it on: a request to the ingest
route missing the header, or carrying one that fails any check above, gets
a 401 *before* the route's own per-user bearer is even looked at — this is
an additional gate, not a replacement for that bearer.

Where the real values come from: Cloudflare Zero Trust dashboard → the
team domain is shown on Settings → Custom Pages (or any Access policy
page) as "Team domain"; the AUD tag is on Access → Applications → (the
ingest app) → Overview, "Application Audience (AUD) Tag". This
deployment's actual values live in the gitignored
`deploy/certs/cloudflare-access-tokens.env` — never committed, never
logged.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx
import jwt
from jwt import PyJWK

from app.config import settings

logger = logging.getLogger(__name__)

# How long a fetched JWKS is trusted before an unconditional refresh, even
# if every `kid` seen so far is still in the cache. Cloudflare rotates keys
# infrequently; this just bounds how long a revoked/rotated key could stay
# trusted if we never happened to see an unknown kid.
_JWKS_CACHE_TTL_SECONDS = 3600
_JWKS_FETCH_TIMEOUT_SECONDS = 10.0

_lock = threading.Lock()
_keys_by_kid: dict[str, PyJWK] = {}
_fetched_at: float = 0.0
_startup_logged = False


class CloudflareAccessError(Exception):
    """A `Cf-Access-Jwt-Assertion` header failed verification."""


def is_configured() -> bool:
    """Both settings must be set for the check to be anything but a no-op."""
    return bool(settings.cf_access_team_domain and settings.cf_access_aud)


def log_startup_status() -> None:
    """Log once, at app startup, whether the check is enabled.

    Idempotent per-process (guards against being called more than once,
    e.g. from a test or a lifespan re-entry) so it never spams the log.
    """
    global _startup_logged
    if _startup_logged:
        return
    _startup_logged = True
    if is_configured():
        logger.info(
            "Cloudflare Access JWT verification: ENABLED "
            f"(team={settings.cf_access_team_domain!r}, aud={settings.cf_access_aud[:8]}...)"
        )
    else:
        logger.info(
            "Cloudflare Access JWT verification: disabled "
            "(HOME_CF_ACCESS_TEAM_DOMAIN / HOME_CF_ACCESS_AUD not both set)"
        )


def _certs_url() -> str:
    return f"https://{settings.cf_access_team_domain}/cdn-cgi/access/certs"


def _fetch_jwks() -> dict:
    resp = httpx.get(_certs_url(), timeout=_JWKS_FETCH_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def _refresh_jwks_locked() -> None:
    """Fetch the team's JWKS and replace the cached key set. Caller must
    hold `_lock`."""
    global _fetched_at
    data = _fetch_jwks()
    keys: dict[str, PyJWK] = {}
    for jwk_dict in data.get("keys", []) or []:
        kid = jwk_dict.get("kid")
        if not kid:
            continue
        try:
            keys[kid] = PyJWK.from_dict(jwk_dict)
        except Exception:  # noqa: BLE001 — one malformed key must not sink the rest
            logger.exception("Cloudflare Access: skipping unparseable JWKS entry")
    _keys_by_kid.clear()
    _keys_by_kid.update(keys)
    _fetched_at = time.monotonic()


def _get_signing_key(kid: str) -> PyJWK:
    """Return the cached key for `kid`, refreshing the JWKS first if the
    cache is stale or the kid isn't (yet) known — a rotated Cloudflare key
    should start working on the next request, not require a restart."""
    with _lock:
        stale = (time.monotonic() - _fetched_at) > _JWKS_CACHE_TTL_SECONDS
        if kid not in _keys_by_kid or stale:
            _refresh_jwks_locked()
        key = _keys_by_kid.get(kid)
        if key is None:
            raise CloudflareAccessError(f"unknown key id {kid!r} in Cloudflare Access JWKS")
        return key


def reset_cache_for_tests() -> None:
    """Test-only: clear the module-level JWKS cache between cases."""
    with _lock:
        _keys_by_kid.clear()
        global _fetched_at
        _fetched_at = 0.0


def verify_access_jwt(token: str) -> dict:
    """Verify a `Cf-Access-Jwt-Assertion` value. Returns the decoded claims
    on success; raises `CloudflareAccessError` on any failure (malformed
    token, unknown key, bad signature, wrong issuer/audience, expired or
    not-yet-valid)."""
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise CloudflareAccessError(f"malformed token: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise CloudflareAccessError("token header carries no kid")

    signing_key = _get_signing_key(kid)

    issuer = f"https://{settings.cf_access_team_domain}"
    try:
        return jwt.decode(
            token,
            key=signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=settings.cf_access_aud,
            options={"require": ["exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise CloudflareAccessError(str(exc)) from exc


def check_request(headers) -> str | None:
    """Apply the check to one request's headers.

    Returns `None` if the request may proceed (check disabled, or the
    header is present and verifies), else a short human-readable reason
    suitable for a 401 body — never the raw exception (which may include
    a fragment of the token or key material).

    `headers` is anything with a case-insensitive `.get(name)` — a
    Starlette `Request.headers` in production, a plain dict in tests.
    """
    if not is_configured():
        return None
    token = headers.get("Cf-Access-Jwt-Assertion")
    if not token:
        return "Missing Cf-Access-Jwt-Assertion header"
    try:
        verify_access_jwt(token)
    except CloudflareAccessError as exc:
        logger.warning(f"Cloudflare Access JWT rejected: {exc}")
        return "Invalid Cloudflare Access JWT"
    return None
