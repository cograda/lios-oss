"""Tests for graceful OAuth refresh-token revocation handling.

Verifies that:
1. `get_credentials` raises NeedsReauthError + sets the token row's
   `needs_reauth_at` when google-auth signals a hard refresh failure
   (e.g. invalid_grant).
2. Transient RefreshErrors are NOT swallowed — they bubble up so the
   scheduler retry-once logic still kicks in.
3. A token already flagged short-circuits without retrying the refresh.
4. `exchange_code` clears the flag on a successful new consent.

These run without a real DB — we hand-roll a mock session + token row.
"""

import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# Historical-corpus's BoQ parser imports openpyxl at module load. It's a real
# prod dep (installed in Docker) but isn't required for this test path — stub
# it before any `app.models` import cascades. Removing this would couple the
# OAuth test to an unrelated integration's dependency footprint.
sys.modules.setdefault("openpyxl", ModuleType("openpyxl"))


@pytest.fixture(autouse=True)
def _patch_model_lookups():
    """Provide minimal stubs for the lazy imports inside oauth.py.

    `get_credentials` and `exchange_code` do `from app.models.tokens import
    OAuthToken` and `from app.models.users import User` respectively. Earlier
    tests in the suite (test_lastfm_sync, test_apple_health) stub these
    modules with non-package ModuleTypes for their own purposes. Those stubs
    leak across tests via sys.modules.

    The mock session in this file ignores the `session.query(X)` argument, so
    we don't need the real model classes — just *something* importable under
    that name. A MagicMock attribute is enough to satisfy the import.
    """
    for name in ("app.models.tokens", "app.models.users"):
        mod = sys.modules.get(name)
        if mod is None:
            mod = ModuleType(name)
            sys.modules[name] = mod
        # Idempotent: only set if missing so the real class wins when present.
        if not hasattr(mod, "OAuthToken") and name.endswith("tokens"):
            mod.OAuthToken = MagicMock()
        if not hasattr(mod, "User") and name.endswith("users"):
            mod.User = MagicMock()
    yield


def _make_token(
    *,
    needs_reauth: bool = False,
    expires_at: datetime | None = None,
):
    """Construct a mock OAuthToken with the fields get_credentials touches."""
    t = MagicMock()
    t.user_id = 1
    t.provider = "google"
    t.account_email = "test@gmail.com"
    t.access_token = "enc_access"
    t.refresh_token = "enc_refresh"
    t.token_type = "Bearer"
    t.scopes = "https://www.googleapis.com/auth/calendar"
    t.expires_at = expires_at or (datetime.now(timezone.utc) - timedelta(hours=1))
    t.needs_reauth_at = datetime.now(timezone.utc) if needs_reauth else None
    t.needs_reauth_reason = "previously flagged" if needs_reauth else None
    return t


def _make_session(token):
    """Mock SQLAlchemy session whose .query().filter_by().first() returns `token`."""
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = token
    return session


def test_hard_refresh_failure_flags_token_and_raises_needs_reauth():
    """invalid_grant must set needs_reauth_at and raise NeedsReauthError."""
    from google.auth.exceptions import RefreshError

    from app.auth.oauth import NeedsReauthError, get_credentials

    token = _make_token()
    session = _make_session(token)

    with patch("app.auth.oauth.decrypt_token", side_effect=lambda x: x), \
         patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh:
        mock_refresh.side_effect = RefreshError(
            "('invalid_grant: Token has been expired or revoked.', ...)"
        )

        with pytest.raises(NeedsReauthError) as exc_info:
            # Credentials.expired is True because expires_at is in the past,
            # and refresh_token is non-None — refresh() will be attempted.
            get_credentials("test@gmail.com", session, user_id=1)

    assert "test@gmail.com" in str(exc_info.value)
    assert token.needs_reauth_at is not None
    assert token.needs_reauth_reason is not None
    assert "invalid_grant" in token.needs_reauth_reason
    session.commit.assert_called()


def test_transient_refresh_failure_propagates_unchanged():
    """Network blips and 5xx must NOT flag the token — let the scheduler retry."""
    from google.auth.exceptions import RefreshError

    from app.auth.oauth import NeedsReauthError, get_credentials

    token = _make_token()
    session = _make_session(token)

    with patch("app.auth.oauth.decrypt_token", side_effect=lambda x: x), \
         patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh:
        # No "invalid_grant" substring → treated as transient.
        mock_refresh.side_effect = RefreshError("Connection reset by peer")

        with pytest.raises(RefreshError):
            get_credentials("test@gmail.com", session, user_id=1)

    # Critically: the flag must NOT have been set on a transient failure.
    assert token.needs_reauth_at is None
    assert token.needs_reauth_reason is None


def test_already_flagged_token_short_circuits():
    """A token with needs_reauth_at set must raise immediately without re-trying."""
    from app.auth.oauth import NeedsReauthError, get_credentials

    token = _make_token(needs_reauth=True)
    session = _make_session(token)

    with patch("app.auth.oauth.decrypt_token", side_effect=lambda x: x), \
         patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh:
        with pytest.raises(NeedsReauthError):
            get_credentials("test@gmail.com", session, user_id=1)

        # Must short-circuit BEFORE attempting any network call.
        mock_refresh.assert_not_called()


def test_successful_exchange_clears_reauth_flag():
    """exchange_code must wipe needs_reauth_at + reason on a fresh consent."""
    from app.auth.oauth import _sign_state, exchange_code

    # State signing derives its key from `settings.oauth_encryption_key`
    # (2026-09-06; was the UI token) — conftest's autouse
    # `_default_encryption_key` already pins one for every test.

    token = _make_token(needs_reauth=True)
    session = _make_session(token)

    # Mock User row lookup
    user_row = MagicMock(id=1, name="alex")
    # Two .first() calls: one for User, one for OAuthToken — order matters.
    session.query.return_value.filter_by.return_value.first.side_effect = [user_row, token]

    state = _sign_state({
        "account": "test@gmail.com",
        "user": "alex",
        "iat": int(datetime.now(timezone.utc).timestamp()),
    })

    fake_response = MagicMock()
    fake_response.json.return_value = {
        "access_token": "new_access",
        "refresh_token": "new_refresh",
        "token_type": "Bearer",
        "scope": "https://www.googleapis.com/auth/calendar",
        "expires_in": 3600,
    }
    fake_response.raise_for_status = MagicMock()

    with patch("app.auth.oauth.encrypt_token", side_effect=lambda x: x), \
         patch("app.auth.oauth.httpx.post", return_value=fake_response), \
         patch("app.models.users.User", create=True):
        exchange_code("code123", state, "http://localhost/callback", session)

    assert token.needs_reauth_at is None
    assert token.needs_reauth_reason is None
