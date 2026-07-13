"""Security utilities for token comparison."""

import hmac


def safe_token_check(provided: str, expected: str) -> bool:
    """Constant-time token comparison.

    Returns False if either argument is empty or None.
    Uses hmac.compare_digest to prevent timing attacks.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)
