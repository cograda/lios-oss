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

# Scopes we request — Calendar and Gmail
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]

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


def _state_signing_key() -> bytes:
    """Derive a stable signing key from the UI token.

    Using ui_token (server-only secret) means we don't need a separate config
    knob, and the signature is invalidated automatically if it's rotated.
    """
    secret = (settings.ui_token or "").encode()
    if not secret:
        raise RuntimeError("HOME_UI_TOKEN must be set to sign OAuth state")
    return hashlib.sha256(b"oauth-state-v1:" + secret).digest()


def _sign_state(payload: dict) -> str:
    """Return a base64url-encoded `<payload_b64>.<sig_b64>` token.

    The state field is otherwise attacker-controllable — without a signature
    a tailnet attacker could forge `{"user":"alex"}` and burn a token meant
    for them into Alex's row.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_state_signing_key(), raw, hashlib.sha256).digest()
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return f"{enc(raw)}.{enc(sig)}"


def _verify_state(state: str) -> dict:
    """Parse and verify a signed state token. Raises ValueError on tamper."""
    try:
        payload_b64, sig_b64 = state.split(".", 1)
    except ValueError as e:
        raise ValueError("malformed state token") from e

    def _pad(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    raw = _pad(payload_b64)
    sig = _pad(sig_b64)
    expected = hmac.new(_state_signing_key(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("state signature invalid")
    return json.loads(raw)


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
        "scope": " ".join(SCOPES),
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
    token.scopes = token_data.get("scope", " ".join(SCOPES))
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
        scopes=token.scopes.split() if token.scopes else SCOPES,
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
