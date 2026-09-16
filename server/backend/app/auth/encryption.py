"""Encrypt/decrypt secrets at rest using Fernet (AES-128-CBC + HMAC).

V4 chunk 3.3: fail-closed. `HOME_OAUTH_ENCRYPTION_KEY` is required in
production (set directly in the server's `.env` — see server/.env.example).
If it's unset, this module no longer silently passes secrets through in
plaintext — it raises `EncryptionKeyMissingError` the moment a real
encrypt/decrypt is attempted.

The raise is deliberately *lazy*, not at import time: importing this module
(or any module that imports it) never touches the key. Only a call to
`encrypt_token()`/`decrypt_token()` that actually needs to do cryptographic
work does — and even then, empty strings and already-plaintext passthrough
values short-circuit before the key is ever consulted. This means test
files that don't exercise encryption never need to set a key just to
collect; tests that do (e.g. anything writing/reading an OAuthToken or a
secret `integration_config` row) need `HOME_OAUTH_ENCRYPTION_KEY` set for
the duration — see `tests/conftest.py::_default_encryption_key` for the
session-wide test default, and `tests/test_encryption.py` for the
fail-closed path itself (which explicitly unsets it).

Fernet ciphertext always starts with 'gAAAAA' — used to detect
already-encrypted values.

Generate a key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

_FERNET_PREFIX = "gAAAAA"


class EncryptionKeyMissingError(RuntimeError):
    """HOME_OAUTH_ENCRYPTION_KEY is unset and a real encrypt/decrypt was attempted."""

    def __init__(self) -> None:
        super().__init__(
            "HOME_OAUTH_ENCRYPTION_KEY is not set — refusing to encrypt or "
            "decrypt secrets (fail-closed, V4 chunk 3.3). Generate one: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )


def _get_fernet():
    """Get a Fernet instance. Raises EncryptionKeyMissingError if no key configured."""
    if not settings.oauth_encryption_key:
        raise EncryptionKeyMissingError()
    from cryptography.fernet import Fernet
    return Fernet(settings.oauth_encryption_key.encode())


def is_encrypted(value: str) -> bool:
    """Check if a string looks like Fernet ciphertext."""
    return value.startswith(_FERNET_PREFIX)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a value. Empty/already-encrypted values pass through untouched.

    Raises EncryptionKeyMissingError if the key is unset and there's real
    work to do (non-empty, not-already-encrypted plaintext).
    """
    if not plaintext:
        return plaintext
    if is_encrypted(plaintext):
        return plaintext
    fernet = _get_fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a value. Empty/non-ciphertext values pass through untouched.

    Raises EncryptionKeyMissingError if the key is unset and the value is
    actually Fernet ciphertext needing decryption.
    """
    if not ciphertext:
        return ciphertext
    if not is_encrypted(ciphertext):
        return ciphertext
    fernet = _get_fernet()
    return fernet.decrypt(ciphertext.encode()).decode()
