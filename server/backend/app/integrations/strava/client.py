"""Strava API v3 wrapper — HTTP only, no DB access.

Reference: https://developers.strava.com/docs/reference/
           https://developers.strava.com/docs/authentication/

Everything here is a plain httpx call. Persistence, token storage and
upserting all live in `sync.py` / `routes.py`, per the integration contract
in `server/docs/writing-an-integration.md`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.errors import NeedsReauthError, PermanentError, TransientError

logger = logging.getLogger(__name__)

API_BASE = "https://www.strava.com/api/v3"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/api/v3/oauth/token"
DEAUTHORIZE_URL = "https://www.strava.com/oauth/deauthorize"

# `activity:read_all`, not `activity:read`.
#
# ⚠️ This is the difference between an archive and a *partial* archive that
# reports success. `activity:read` silently omits every activity whose
# visibility is "Only You", and omits privacy-zone geometry from the rest.
# There is no error, no warning and no count discrepancy to notice — the
# endpoint simply returns fewer activities than exist. For a historical
# backfill whose entire purpose is completeness, the read-only-but-complete
# scope is the correct one.
SCOPES = "activity:read_all,profile:read_all"

# Strava's documented ceiling is 200; asking for more is not an error, it is
# silently clamped — which would make a "did I get a full page?" pagination
# test wrong in a way that terminates the backfill early.
MAX_PER_PAGE = 200

# Strava's default application limits: 200 requests per 15 minutes and 2,000
# per day (100/1,000 for applications registered more recently). A full
# historical backfill at 200 activities per request is a few dozen calls for
# any realistic history, so this matters only if a backfill is restarted in a
# loop. `RateLimited` is raised rather than slept through so the caller can
# checkpoint and resume — see `sync.backfill_activities`.
RATE_LIMIT_STATUS = 429

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class RateLimited(TransientError):
    """Strava returned 429. Transient by definition — the window resets.

    Carries the usage headers so the caller can log how close to the limit it
    got, rather than reporting a bare "rate limited" with no way to tell a
    15-minute window from a daily exhaustion.
    """

    def __init__(self, message: str, *, limit: str | None = None, usage: str | None = None):
        super().__init__(message)
        self.limit = limit
        self.usage = usage


def _raise_for_status(
    response: httpx.Response, context: str, *, account: str = "unknown"
) -> None:
    """Classify a Strava error response onto comar's error hierarchy.

    The classification matters to the scheduler: `TransientError` is retried
    and left off the dashboard, `PermanentError` stops the sync and surfaces,
    and `NeedsReauthError` additionally stamps `OAuthToken.needs_reauth_at`
    so the scheduler stops re-trying a dead grant every 30 minutes forever.
    """
    if response.status_code == RATE_LIMIT_STATUS:
        raise RateLimited(
            f"{context}: Strava rate limit reached",
            limit=response.headers.get("X-RateLimit-Limit"),
            usage=response.headers.get("X-RateLimit-Usage"),
        )
    if response.status_code == 401:
        # A 401 on a *refreshed* token means the athlete revoked the grant in
        # Strava's settings — no amount of retrying recovers it.
        #
        # `NeedsReauthError(account, reason)` — the account travels with the
        # error so log lines and SSE events can name the dead athlete without
        # the caller re-deriving it from the original sync arguments.
        raise NeedsReauthError(account, f"{context}: Strava rejected the token (401)")
    if response.status_code == 403:
        body = response.text[:200]
        raise PermanentError(f"{context}: Strava forbade the request (403) — {body}")
    if 400 <= response.status_code < 500:
        raise PermanentError(
            f"{context}: Strava returned {response.status_code} — {response.text[:200]}"
        )
    if response.status_code >= 500:
        raise TransientError(f"{context}: Strava returned {response.status_code}")


def build_authorize_url(
    *, client_id: str, redirect_uri: str, state: str, force: bool = True
) -> str:
    """URL to send the athlete to, to grant access.

    `approval_prompt=force` by default. `auto` would skip the consent screen
    for an athlete who has already approved this application — convenient,
    but it also skips it when the *previously granted scopes were narrower*,
    handing back a token that cannot see private activities while looking
    entirely successful. Forcing the prompt costs one extra click and removes
    that failure.
    """
    import urllib.parse

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "force" if force else "auto",
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(*, client_id: str, client_secret: str, code: str) -> dict[str, Any]:
    """Trade an authorization code for the first access/refresh token pair.

    Returns Strava's raw token payload: `access_token`, `refresh_token`,
    `expires_at` (epoch seconds), `token_type`, and an `athlete` summary.
    """
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        _raise_for_status(response, "Strava code exchange")
        return response.json()


def refresh_access_token(
    *, client_id: str, client_secret: str, refresh_token: str, account: str = "unknown"
) -> dict[str, Any]:
    """Exchange a refresh token for a fresh access token.

    ⚠️ **Strava rotates the refresh token.** The response contains a
    `refresh_token` field which may differ from the one sent, and once a
    rotated token has been issued the old one stops working. Persisting only
    the `access_token` from this response leaves a row that authenticates
    fine for six hours and is then permanently dead, with the failure
    appearing hours later and nowhere near the code that caused it. The
    caller MUST write back every field — `sync._refresh_if_needed` does.

    Unlike Google, this is not an occasional repair path: Strava access
    tokens expire after six hours, so a 30-minute sync refreshes several
    times a day and the rotation is exercised constantly.
    """
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        if response.status_code == 400:
            # Strava answers a revoked/invalid refresh token with 400, not
            # 401. Classifying it as a generic PermanentError would leave the
            # scheduler retrying a grant that will never come back.
            raise NeedsReauthError(
                account,
                "Strava refused the refresh token — the grant was revoked or rotated away",
            )
        _raise_for_status(response, "Strava token refresh", account=account)
        return response.json()


def fetch_activities(
    *,
    access_token: str,
    before: int | None = None,
    after: int | None = None,
    page: int = 1,
    per_page: int = MAX_PER_PAGE,
    account: str = "unknown",
) -> list[dict[str, Any]]:
    """One page of `GET /athlete/activities`, newest first.

    `before` / `after` are epoch seconds and filter on activity start time.
    Returns the raw SummaryActivity dicts; parsing is `sync.py`'s job.
    """
    params: dict[str, Any] = {"per_page": min(per_page, MAX_PER_PAGE), "page": page}
    if before is not None:
        params["before"] = int(before)
    if after is not None:
        params["after"] = int(after)

    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.get(
            f"{API_BASE}/athlete/activities",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        _raise_for_status(response, "Strava fetch_activities", account=account)
        payload = response.json()

    if not isinstance(payload, list):
        raise PermanentError(
            f"Strava fetch_activities: expected a list, got {type(payload).__name__}"
        )
    return payload


def fetch_athlete(*, access_token: str) -> dict[str, Any]:
    """`GET /athlete` — the authenticated athlete, used to label the token row."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.get(
            f"{API_BASE}/athlete",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        _raise_for_status(response, "Strava fetch_athlete")
        return response.json()
