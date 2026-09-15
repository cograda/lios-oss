"""Google OAuth2 flow — supports multiple accounts.

Each account gets its own token row in the oauth_tokens table. The flow:

1. User visits /api/auth/google/login?account=user@gmail.com
2. Redirected to Google consent screen
3. Google redirects back to /api/auth/google/callback with code + state
4. We exchange the code for tokens and store them

Uses httpx for the token exchange to avoid google_auth_oauthlib's
automatic PKCE enforcement, which complicates the stateless callback.
"""

import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.auth.encryption import decrypt_token, encrypt_token
from app.config import settings
# `NeedsReauthError` now lives in app/errors.py (as a `PermanentError`
# subclass) alongside the rest of the sync error hierarchy. Re-exported here
# so `from app.auth.oauth import NeedsReauthError` keeps working everywhere.
from app.errors import NeedsReauthError  # noqa: F401

# `OAuthToken` is imported lazily inside the functions that touch it. Top-level
# imports here pull in app/models/__init__.py, which transitively imports the
# google_calendar integration, which imports get_credentials from this file —
# a circular load that only worked previously because production startup orders
# integration imports before any test-style cold-import of app.auth.oauth.

logger = logging.getLogger(__name__)

def _oauth_scopes() -> list[str]:
    """Union of every integration's declared Google OAuth scopes.

    V4 chunk 3.3: replaces the single hard-coded `SCOPES` list — each
    integration that needs Google OAuth now declares its own scopes in its
    manifest (`oauth.scopes`; see google_calendar, google_mail, snags), and
    a consent still requests all of them in one go (current actual
    behavior, just relocated — a single Google account signs in once and
    gets everything every integration needs, same as before).

    Computed at request time, not cached, so a newly-added integration's
    scopes take effect without a restart-order dependency.
    """
    from app.plugin.validate import discover_manifests

    scopes: set[str] = set()
    for manifest in discover_manifests().values():
        if manifest.oauth is not None:
            scopes.update(manifest.oauth.scopes)
    return sorted(scopes)


# Additional scopes to add as integrations are built
PHOTOS_SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


# Substrings in google.auth.exceptions.RefreshError that mean "this refresh
# token is permanently dead." Anything not in this set is treated as a
# transient network/server issue and bubbles up unchanged so the retry-once
# logic in the scheduler still runs.
_HARD_REFRESH_FAILURES = (
    "invalid_grant",          # token revoked, expired, or password changed
    "Token has been expired or revoked",
    "Token has been revoked",
    "unauthorized_client",    # OAuth client_id disabled
    "invalid_client",         # OAuth client_id mismatch
)


_STATE_KEY_INFO = b"lios oauth-state-signing v2"


def _state_signing_key() -> bytes:
    """Derive the OAuth `state` HMAC key from the at-rest encryption key.

    2026-09-06 — one credential: the per-user bearer. This used to be
    `sha256("oauth-state-v1:" + HOME_UI_TOKEN)`, which tied the Google consent
    flow's integrity to the dashboard's shared password: rotate or retire that
    token and every in-flight consent broke, and the state signature was only
    as secret as a cookie value typed into a browser. `HOME_OAUTH_ENCRYPTION_KEY`
    is the one secret the server already *must* hold (it decrypts the very
    refresh tokens this flow produces — `app/auth/encryption.py` is
    fail-closed on it), so the state key is derived from it with HKDF under a
    fixed, purpose-naming `info` string. HKDF rather than reusing the Fernet
    key bytes directly, so the signing key and the encryption key are
    independent values even though they share a root — a leak of one does not
    hand over the other.

    A missing key still raises, never signs with an empty secret: an
    unsigned/forgeable state is exactly the tampering `_sign_state`'s
    docstring describes.
    """
    root = (settings.oauth_encryption_key or "").encode()
    if not root:
        raise RuntimeError("HOME_OAUTH_ENCRYPTION_KEY must be set to sign OAuth state")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_STATE_KEY_INFO,
    ).derive(root)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign_payload(payload: dict, key: bytes) -> str:
    """`<payload_b64>.<sig_b64>` — HMAC-SHA256 over the canonical JSON."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(key, raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def _verify_payload(token: str, key: bytes) -> dict:
    """Inverse of `_sign_payload`. Raises ValueError on any tamper/malformation."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError as e:
        raise ValueError("malformed signed token") from e
    try:
        raw = _unb64(payload_b64)
        sig = _unb64(sig_b64)
    except (ValueError, TypeError) as e:  # bad base64 / non-ascii
        raise ValueError("malformed signed token") from e
    expected = hmac.new(key, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("signature invalid")
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise ValueError("malformed signed token") from e
    if not isinstance(payload, dict):
        raise ValueError("malformed signed token")
    return payload


def _sign_state(payload: dict) -> str:
    """Return a base64url-encoded `<payload_b64>.<sig_b64>` token.

    The state field is otherwise attacker-controllable — without a signature
    a tailnet attacker could forge `{"user":"alex"}` and burn a token meant
    for them into Alex's row.
    """
    return _sign_payload(payload, _state_signing_key())


def _verify_state(state: str) -> dict:
    """Parse and verify a signed state token. Raises ValueError on tamper."""
    try:
        return _verify_payload(state, _state_signing_key())
    except ValueError as e:
        # Keep the historical wording callers/tests match on.
        msg = str(e)
        if "signature" in msg:
            raise ValueError("state signature invalid") from e
        raise ValueError("malformed state token") from e


# ---------------------------------------------------------------------------
# Signed `start` for GET /api/auth/google/login (2026-09-07)
# ---------------------------------------------------------------------------
#
# `google/login` is exempt from the dashboard session on purpose (the re-auth
# link is followed on the Tailscale hostname, where the `comar.lab` cookie is
# not sent — see `AUTH_EXEMPT` in app/main.py). Exempt used to mean *anyone on
# the tailnet could START a Google grant naming any user*; only the callback
# was protected. Now the route also requires `start`: a short-lived HMAC over
# (account, user, expiry) that only a session-authenticated route (or an MCP
# tool running as a bearer-authenticated user) can mint. The link still works
# across domains because the proof travels in the URL, not in a cookie.
#
# Its own HKDF `info` label, so a `state` token can never be replayed as a
# `start` token or vice versa even though both derive from the same root.

_LOGIN_START_KEY_INFO = b"lios oauth-login-start v1"

#: How long a minted `start` stays valid. A banner link is clicked within
#: seconds; ten minutes matches the strava `state` TTL and bounds replay.
LOGIN_START_TTL_SECONDS = 600


def _login_start_signing_key() -> bytes:
    root = (settings.oauth_encryption_key or "").encode()
    if not root:
        raise RuntimeError("HOME_OAUTH_ENCRYPTION_KEY must be set to sign OAuth login start")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_LOGIN_START_KEY_INFO,
    ).derive(root)


def sign_login_start(account_email: str, user_name: str, *, now: datetime | None = None) -> str:
    """Mint the `start` proof for `google/login?account=&user=`.

    Call this ONLY from a context that has already authenticated the person
    building the link (a session-gated route, or an MCP tool running as a
    bearer user) — the proof is what stands in for the session on the
    exempt route.
    """
    now = now or datetime.now(timezone.utc)
    exp = int(now.timestamp()) + LOGIN_START_TTL_SECONDS
    return _sign_payload(
        {"account": account_email, "user": user_name, "exp": exp},
        _login_start_signing_key(),
    )


def verify_login_start(
    start: str, *, account_email: str, user_name: str, now: datetime | None = None,
) -> None:
    """Raise ValueError unless `start` is a valid, unexpired proof for exactly
    this (account, user) pair. Returns None on success."""
    if not start:
        raise ValueError("start is required")
    payload = _verify_payload(start, _login_start_signing_key())
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise ValueError("start has no expiry")
    now = now or datetime.now(timezone.utc)
    if int(now.timestamp()) >= exp:
        raise ValueError("start has expired")
    if payload.get("account") != account_email or payload.get("user") != user_name:
        raise ValueError("start does not match account/user")


def google_login_url(account_email: str, user_name: str) -> str:
    """The one place a `google/login` link is built — with its signed `start`.

    Relative, so it works on whichever host (comar.lab or the tailnet name)
    the page was served from; the `start` proof is what makes it valid on
    either, since the session cookie does not travel.
    """
    q = urllib.parse.urlencode({
        "account": account_email,
        "user": user_name,
        "start": sign_login_start(account_email, user_name),
    })
    return f"/api/auth/google/login?{q}"


def create_auth_url(
    account_email: str, redirect_uri: str, *, user_name: str = "alex"
) -> str:
    """Generate Google OAuth authorization URL.

    Builds the URL manually to avoid PKCE code_challenge being added
    automatically by google_auth_oauthlib. The user_name is round-tripped
    via state so the callback knows which User row to attach the token to.
    State is HMAC-signed so an attacker cannot forge an alternate (user,
    account) pairing through the callback.
    """
    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_state({
        "account": account_email,
        "user": user_name,
        "iat": issued_at,
    })
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_oauth_scopes()),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "login_hint": account_email,
    }
    return f"{GOOGLE_AUTH_URI}?{urllib.parse.urlencode(params)}"


def exchange_code(
    code: str, state: str, redirect_uri: str, session: Session
):
    """Exchange authorization code for tokens and store in DB.

    Verifies the state signature first — without this an attacker could
    forge `state={"user":"alex","account":"evil@gmail.com"}` and have the
    callback write their own Google tokens into Alex's row.
    """
    from app.models.tokens import OAuthToken
    from app.models.users import User

    state_data = _verify_state(state)
    account_email = state_data["account"]
    user_name = state_data["user"]

    # Reject state tokens older than 1 hour — OAuth flows complete in seconds,
    # so anything older is a replay.
    issued_at = state_data.get("iat", 0)
    if issued_at and (datetime.now(timezone.utc).timestamp() - issued_at) > 3600:
        raise ValueError("state token expired")

    user_row = session.query(User).filter_by(name=user_name).first()
    if not user_row:
        raise ValueError(f"OAuth state references unknown user '{user_name}'")

    # Exchange code for tokens via direct HTTP POST
    resp = httpx.post(GOOGLE_TOKEN_URI, data={
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    token_data = resp.json()

    # Upsert token row scoped to this user.
    token = (
        session.query(OAuthToken)
        .filter_by(
            user_id=user_row.id,
            provider="google",
            account_email=account_email,
        )
        .first()
    )
    if token is None:
        token = OAuthToken(
            user_id=user_row.id,
            provider="google",
            account_email=account_email,
        )
        session.add(token)

    token.access_token = encrypt_token(token_data["access_token"])
    token.refresh_token = encrypt_token(token_data.get("refresh_token") or "") or token.refresh_token
    token.token_type = token_data.get("token_type", "Bearer")
    # `or`, not `.get(..., default)` — the latter evaluates the default
    # eagerly (a discover_manifests() walk) even on the common path where
    # Google's response already includes a `scope` field.
    token.scopes = token_data.get("scope") or " ".join(_oauth_scopes())
    if "expires_in" in token_data:
        token.expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])
    # Successful consent clears any prior revocation flag — the new tokens
    # supersede whatever was there. Without this clear, the dashboard banner
    # would linger after re-auth.
    token.needs_reauth_at = None
    token.needs_reauth_reason = None

    session.commit()
    logger.info(f"Stored OAuth token for {account_email}")
    return token


def get_credentials(account_email: str, session: Session, *, user_id: int):
    """Load stored credentials for an account, refreshing if needed.

    `user_id` is REQUIRED — `OAuthToken` is per-user (UserOwnedMixin), and
    filtering only on `(provider, account_email)` would let user N read
    user M's mailbox/calendar. Callers in tool handlers should pass
    `current_user_id()`; sync paths pass `token.user_id` from the row they
    already hold.

    Returns a google.oauth2.credentials.Credentials object ready to use,
    or None if no token is stored for this (user_id, account_email).
    """
    from google.oauth2.credentials import Credentials

    from app.models.tokens import OAuthToken

    token = (
        session.query(OAuthToken)
        .filter_by(user_id=user_id, provider="google", account_email=account_email)
        .first()
    )
    if token is None:
        return None

    # Short-circuit if a previous refresh attempt flagged the token as dead.
    # Without this, every scheduled sync re-enters the broken refresh path,
    # which costs Google API quota and floods the logs with stack traces.
    # The flag is cleared by exchange_code() on successful re-consent.
    if token.needs_reauth_at is not None:
        raise NeedsReauthError(account_email, token.needs_reauth_reason or "previously flagged")

    creds = Credentials(
        token=decrypt_token(token.access_token),
        refresh_token=decrypt_token(token.refresh_token) if token.refresh_token else None,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=token.scopes.split() if token.scopes else _oauth_scopes(),
        expiry=token.expires_at.replace(tzinfo=None) if token.expires_at else None,
    )

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request

        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # google-auth surfaces the OAuth `error` field in str(exc). Match
            # against known terminal-failure substrings — transient network /
            # 5xx errors don't include these and should bubble up so the
            # scheduler's retry-once kicks in.
            err_str = str(exc)
            if any(s in err_str for s in _HARD_REFRESH_FAILURES):
                reason = err_str[:200]
                token.needs_reauth_at = datetime.now(timezone.utc)
                token.needs_reauth_reason = reason
                session.commit()
                logger.warning(
                    f"OAuth token for {account_email} flagged needs_reauth: {reason}"
                )
                raise NeedsReauthError(account_email, reason) from exc
            # Transient — re-raise unchanged so the scheduler treats it as
            # a normal failure (retryable).
            raise

        # Update stored token
        token.access_token = encrypt_token(creds.token)
        token.expires_at = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None
        session.commit()
        logger.info(f"Refreshed token for {account_email}")

    return creds
