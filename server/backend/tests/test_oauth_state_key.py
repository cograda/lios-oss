"""Google OAuth `state` signing key — derived from the at-rest encryption key,
not the dashboard token (2026-09-06, "one credential: the per-user bearer").

`_state_signing_key()` used to be `sha256("oauth-state-v1:" + HOME_UI_TOKEN)`,
which coupled the consent flow's integrity to the shared dashboard password.
It is now HKDF over `HOME_OAUTH_ENCRYPTION_KEY` — the one secret the server
must already hold to decrypt the refresh tokens the flow produces. Unit tier:
no DB, no network.
"""

import pytest

from app.auth import oauth
from app.config import settings


def test_key_derives_from_the_encryption_key_alone(monkeypatch):
    monkeypatch.setattr(settings, "oauth_encryption_key", "root-key-A")
    k1 = oauth._state_signing_key()
    # `settings.ui_token` no longer exists at all (removed 2026-09-06) — the
    # key can only come from the encryption key. Rotating that must change it.
    assert not hasattr(settings, "ui_token")
    monkeypatch.setattr(settings, "oauth_encryption_key", "root-key-B")
    assert oauth._state_signing_key() != k1


def test_key_is_not_the_raw_encryption_key_bytes(monkeypatch):
    """HKDF, not reuse: the signing key and the Fernet key share a root but
    are independent values, so leaking one is not leaking the other."""
    import hashlib

    monkeypatch.setattr(settings, "oauth_encryption_key", "root-key-A")
    key = oauth._state_signing_key()
    assert len(key) == 32
    assert key != b"root-key-A"
    assert key != hashlib.sha256(b"root-key-A").digest()
    assert key != hashlib.sha256(b"oauth-state-v1:root-key-A").digest()


def test_missing_encryption_key_raises_rather_than_signing_with_empty_secret(monkeypatch):
    monkeypatch.setattr(settings, "oauth_encryption_key", "")
    with pytest.raises(RuntimeError, match="HOME_OAUTH_ENCRYPTION_KEY"):
        oauth._state_signing_key()
    with pytest.raises(RuntimeError):
        oauth._sign_state({"user": "alex"})


def test_sign_and_verify_round_trip_and_tamper_detection(monkeypatch):
    monkeypatch.setattr(settings, "oauth_encryption_key", "root-key-A")
    state = oauth._sign_state({"user": "alex", "account": "c@example.com"})
    assert oauth._verify_state(state) == {"user": "alex", "account": "c@example.com"}

    import base64
    import json

    _, sig_b64 = state.split(".", 1)
    forged_raw = json.dumps(
        {"user": "sam", "account": "c@example.com"}, separators=(",", ":"), sort_keys=True
    ).encode()
    forged = base64.urlsafe_b64encode(forged_raw).rstrip(b"=").decode() + "." + sig_b64
    with pytest.raises(ValueError):
        oauth._verify_state(forged)


def test_state_signed_under_the_old_ui_token_scheme_no_longer_verifies(monkeypatch):
    """In-flight consents started before this change are invalidated once —
    a state signed with the old `sha256("oauth-state-v1:" + ui_token)` key
    must fail verification, not be silently accepted by a fallback path."""
    import base64
    import hashlib
    import hmac
    import json

    monkeypatch.setattr(settings, "oauth_encryption_key", "root-key-A")
    raw = json.dumps({"user": "alex"}, separators=(",", ":"), sort_keys=True).encode()
    old_key = hashlib.sha256(b"oauth-state-v1:" + b"dashboard-1").digest()
    sig = hmac.new(old_key, raw, hashlib.sha256).digest()
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    old_state = f"{enc(raw)}.{enc(sig)}"

    with pytest.raises(ValueError):
        oauth._verify_state(old_state)
