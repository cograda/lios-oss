"""At-rest hashing for bearer secrets (V4 chunk 2.3 — token hardening).

`client_tokens` and `mcp_access_tokens` used to store the raw bearer value
and look it up by SQL equality — a DB read (backup leak, SQL injection,
careless `SELECT *` in a debugger) was a full impersonation of every user.

We store `sha256(token)` hex-encoded instead, indexed for O(1) lookup, and
compare by hash equality. This is constant-time *by construction*: the
attacker doesn't hold the hash, so there's nothing to time an incremental
guess against (unlike comparing two attacker-controlled strings, which is
what `app.auth.utils.safe_token_check` exists for). `token_last4` is stored
alongside purely for display/logging (admin UI previews, 401 log lines) —
never used for auth decisions.
"""

import hashlib


def hash_token(token: str) -> str:
    """sha256(token) as lowercase hex."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_last4(token: str) -> str:
    """Last 4 characters of a bearer, for display/log lines. Never for auth."""
    return token[-4:] if token else ""
