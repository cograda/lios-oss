"""Encrypt/decrypt OAuth tokens at rest using Fernet (AES-128-CBC + HMAC).

If HOME_OAUTH_ENCRYPTION_KEY is not set, tokens pass through unchanged.
Fernet ciphertext always starts with 'gAAAAA' — used to detect already-encrypted values.

Generate a key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

_FERNET_PREFIX = "gAAAAA"

# Set on the first call to encrypt_token() with no key configured, so the
# warning below fires once at first use rather than spamming logs on every
# OAuth token write.
_warned_no_key = False


def _get_fernet():
    """Get Fernet instance, or None if no key configured."""
    if not settings.oauth_encryption_key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(settings.oauth_encryption_key.encode())


def is_encrypted(value: str) -> bool:
    """Check if a string looks like Fernet ciphertext."""
    return value.startswith(_FERNET_PREFIX)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token value. Returns unchanged if no key set or already encrypted."""
    if not plaintext:
        return plaintext
    if is_encrypted(plaintext):
        return plaintext
    fernet = _get_fernet()
    if fernet is None:
        global _warned_no_key
        if not _warned_no_key:
            logger.warning(
                "HOME_OAUTH_ENCRYPTION_KEY is not set — OAuth tokens (Google "
                "Calendar/Gmail) are being stored in PLAINTEXT. Set "
                "HOME_OAUTH_ENCRYPTION_KEY to enable encryption at rest "
                "(generate one: python -c \"from cryptography.fernet import "
                "Fernet; print(Fernet.generate_key().decode())\")."
            )
            _warned_no_key = True
        return plaintext
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a token value. Returns unchanged if not encrypted or no key set."""
    if not ciphertext:
        return ciphertext
    if not is_encrypted(ciphertext):
        return ciphertext
    fernet = _get_fernet()
    if fernet is None:
        logger.warning("Encrypted token found but no encryption key configured — cannot decrypt")
        return ciphertext
    return fernet.decrypt(ciphertext.encode()).decode()
