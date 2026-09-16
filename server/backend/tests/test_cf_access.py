"""Cloudflare Access JWT verification (lios#139).

A `CF-Access-Client-Id` allow-list shipped and was reverted the same
evening (PR #64/#65) because Access never forwards the service token's
client id to the origin. This suite covers the replacement: verifying the
signed JWT Access DOES forward, in `Cf-Access-Jwt-Assertion`.

Signs its own tokens against a throwaway RSA keypair generated in-process,
and stands in for Cloudflare's JWKS endpoint by monkeypatching
`app.auth.cf_access._fetch_jwks` — no network access, and no dependency on
Cloudflare being reachable for these tests to mean anything.
"""

from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.auth import cf_access

TEAM_DOMAIN = "testteam.cloudflareaccess.com"
AUD = "aud-ingest-app-1234567890abcdef"
KID = "test-kid-1"


def _keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _jwk_for(public_key, kid: str) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return jwk


def _sign(private_key, kid: str, *, iss: str, aud: str, exp_delta: int = 3600, nbf_delta: int | None = None, extra: dict | None = None) -> str:
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "exp": now + exp_delta, "iat": now}
    if nbf_delta is not None:
        claims["nbf"] = now + nbf_delta
    if extra:
        claims.update(extra)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Both settings set — the "check is on" case for every test in this
    file except the ones that explicitly test the unset/no-op path."""
    monkeypatch.setattr(cf_access.settings, "cf_access_team_domain", TEAM_DOMAIN)
    monkeypatch.setattr(cf_access.settings, "cf_access_aud", AUD)
    cf_access.reset_cache_for_tests()
    yield
    cf_access.reset_cache_for_tests()


@pytest.fixture
def keypair():
    return _keypair()


@pytest.fixture
def fake_jwks(keypair, monkeypatch):
    """Serve a one-key JWKS matching `keypair` whenever the module tries to
    fetch Cloudflare's certs endpoint."""
    _private, public = keypair
    jwks = {"keys": [_jwk_for(public, KID)]}
    calls = {"count": 0}

    def _fetch():
        calls["count"] += 1
        return jwks

    monkeypatch.setattr(cf_access, "_fetch_jwks", _fetch)
    return calls


class TestValidToken:
    def test_valid_token_passes(self, keypair, fake_jwks):
        private_key, _public = keypair
        token = _sign(private_key, KID, iss=f"https://{TEAM_DOMAIN}", aud=AUD)

        claims = cf_access.verify_access_jwt(token)

        assert claims["aud"] == AUD

    def test_valid_token_passes_via_check_request(self, keypair, fake_jwks):
        private_key, _public = keypair
        token = _sign(private_key, KID, iss=f"https://{TEAM_DOMAIN}", aud=AUD)

        result = cf_access.check_request({"Cf-Access-Jwt-Assertion": token})

        assert result is None

    def test_unknown_kid_triggers_a_refetch(self, keypair, monkeypatch):
        """The cache starts empty, so the very first verification for any
        kid must trigger exactly one fetch — this is the "refresh on
        unknown kid" behaviour the issue asks for."""
        private_key, public = keypair
        jwks = {"keys": [_jwk_for(public, KID)]}
        calls = {"count": 0}

        def _fetch():
            calls["count"] += 1
            return jwks

        monkeypatch.setattr(cf_access, "_fetch_jwks", _fetch)
        token = _sign(private_key, KID, iss=f"https://{TEAM_DOMAIN}", aud=AUD)

        cf_access.verify_access_jwt(token)

        assert calls["count"] == 1

        # A second token with the SAME kid must not refetch — the cache
        # should serve it.
        token2 = _sign(private_key, KID, iss=f"https://{TEAM_DOMAIN}", aud=AUD)
        cf_access.verify_access_jwt(token2)
        assert calls["count"] == 1


class TestWrongAudience:
    def test_wrong_aud_is_rejected(self, keypair, fake_jwks):
        private_key, _public = keypair
        token = _sign(private_key, KID, iss=f"https://{TEAM_DOMAIN}", aud="some-other-app")

        with pytest.raises(cf_access.CloudflareAccessError):
            cf_access.verify_access_jwt(token)

    def test_wrong_aud_is_401_via_check_request(self, keypair, fake_jwks):
        private_key, _public = keypair
        token = _sign(private_key, KID, iss=f"https://{TEAM_DOMAIN}", aud="some-other-app")

        result = cf_access.check_request({"Cf-Access-Jwt-Assertion": token})

        assert result == "Invalid Cloudflare Access JWT"


class TestWrongIssuer:
    def test_wrong_iss_is_rejected(self, keypair, fake_jwks):
        private_key, _public = keypair
        token = _sign(private_key, KID, iss="https://not-the-team.cloudflareaccess.com", aud=AUD)

        with pytest.raises(cf_access.CloudflareAccessError):
            cf_access.verify_access_jwt(token)


class TestExpired:
    def test_expired_token_is_rejected(self, keypair, fake_jwks):
        private_key, _public = keypair
        token = _sign(private_key, KID, iss=f"https://{TEAM_DOMAIN}", aud=AUD, exp_delta=-60)

        with pytest.raises(cf_access.CloudflareAccessError):
            cf_access.verify_access_jwt(token)

    def test_not_yet_valid_token_is_rejected(self, keypair, fake_jwks):
        private_key, _public = keypair
        token = _sign(
            private_key, KID, iss=f"https://{TEAM_DOMAIN}", aud=AUD,
            nbf_delta=3600,
        )

        with pytest.raises(cf_access.CloudflareAccessError):
            cf_access.verify_access_jwt(token)


class TestBadSignature:
    def test_signature_from_a_different_key_is_rejected(self, keypair, fake_jwks):
        """Sign with a SECOND keypair whose public half never appears in the
        served JWKS. Real bad-signature case (token claims a kid that IS
        published, but the bytes weren't produced by that key)."""
        _served_private, served_public = keypair
        other_private, _other_public = _keypair()

        # Token header claims the kid that IS in the JWKS, but is actually
        # signed by the other (unpublished) key.
        token = _sign(other_private, KID, iss=f"https://{TEAM_DOMAIN}", aud=AUD)

        with pytest.raises(cf_access.CloudflareAccessError):
            cf_access.verify_access_jwt(token)

    def test_unknown_kid_with_no_matching_key_anywhere_is_rejected(self, fake_jwks):
        other_private, _other_public = _keypair()
        token = _sign(other_private, "totally-unknown-kid", iss=f"https://{TEAM_DOMAIN}", aud=AUD)

        with pytest.raises(cf_access.CloudflareAccessError):
            cf_access.verify_access_jwt(token)

    def test_malformed_token_is_rejected(self, fake_jwks):
        with pytest.raises(cf_access.CloudflareAccessError):
            cf_access.verify_access_jwt("not-a-jwt-at-all")


class TestMissingHeader:
    def test_missing_header_is_401_reason(self, fake_jwks):
        result = cf_access.check_request({})

        assert result == "Missing Cf-Access-Jwt-Assertion header"



class TestBothSettingsUnset:
    def test_disabled_ignores_header_entirely(self, monkeypatch):
        monkeypatch.setattr(cf_access.settings, "cf_access_team_domain", "")
        monkeypatch.setattr(cf_access.settings, "cf_access_aud", "")

        assert cf_access.is_configured() is False
        # Even a garbage header must not matter — the check is a no-op.
        result = cf_access.check_request({"Cf-Access-Jwt-Assertion": "garbage-not-even-a-jwt"})
        assert result is None

    def test_disabled_with_missing_header_also_a_noop(self, monkeypatch):
        monkeypatch.setattr(cf_access.settings, "cf_access_team_domain", "")
        monkeypatch.setattr(cf_access.settings, "cf_access_aud", "")

        assert cf_access.check_request({}) is None

    def test_only_team_domain_set_is_still_disabled(self, monkeypatch):
        monkeypatch.setattr(cf_access.settings, "cf_access_team_domain", TEAM_DOMAIN)
        monkeypatch.setattr(cf_access.settings, "cf_access_aud", "")
        assert cf_access.is_configured() is False

    def test_only_aud_set_is_still_disabled(self, monkeypatch):
        monkeypatch.setattr(cf_access.settings, "cf_access_team_domain", "")
        monkeypatch.setattr(cf_access.settings, "cf_access_aud", AUD)
        assert cf_access.is_configured() is False


class TestStartupLogging:
    def test_logs_once_per_process(self, monkeypatch, caplog):
        monkeypatch.setattr(cf_access, "_startup_logged", False)
        with caplog.at_level("INFO", logger="app.auth.cf_access"):
            cf_access.log_startup_status()
            cf_access.log_startup_status()

        matching = [r for r in caplog.records if "Cloudflare Access JWT verification" in r.message]
        assert len(matching) == 1
