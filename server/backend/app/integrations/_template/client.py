"""External API client for the `_template` scaffold integration — V4 chunk 4.3e.

Wraps the (hypothetical) template external service's HTTP API. This is the
one place in the package that talks to the outside world — `sync.py`'s
`pull_items()` calls `fetch_items()` below and does nothing else network-
related; `store_items()` (also in sync.py) does nothing but DB writes. That
split (fetch here, no DB; persist in sync.py, no I/O) is what lets
`app.plugin.bases.SourceIntegration.sync()` call them independently and
still get correct fan-out/error-handling for free.

HTTP error classification: never hand-roll a status-code -> retry/no-retry
mapping in a new client — delegate to `app.plugin.sync_runtime.classify_exc`
(V4 chunk 3.2), which every real integration's client.py already uses
(`google_calendar`, `google_mail`, `sheets`, `lastfm`). It turns a caught
exception into a `TransientError` (retry) or `PermanentError` (don't retry,
surface to the user) — see that module's docstring for the exact rules
(429/5xx/network-with-no-status -> transient; other 4xx -> permanent;
401/403 -> `NeedsReauthError` if you pass `provider="google"` and an
`account_email`, otherwise permanent).
"""

from __future__ import annotations

import logging

import httpx

from app.plugin.sync_runtime import classify_exc

logger = logging.getLogger(__name__)

BASE_URL = "https://template.example.invalid/api/v1"


def fetch_items(api_key: str, *, page_size: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
    """Fetch one page of items from the template external service.

    Returns `(records, next_cursor)`. `next_cursor` is `None` when there's
    nothing more to page through — `sync.py`'s `pull_items()` decides what
    (if anything) to do with it (this scaffold doesn't opt into
    `SyncCursor` bookkeeping; see that module's comment for why).

    No DB access here — this function's only job is "talk to the external
    system and hand back plain data". Never import `app.db` or a session
    into a client.py.
    """
    params: dict[str, str | int] = {"page_size": page_size}
    if cursor:
        params["cursor"] = cursor

    try:
        response = httpx.get(
            f"{BASE_URL}/items",
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        response.raise_for_status()
    except Exception as exc:
        # provider="generic" (not "google") since this is a plain bearer-token
        # API, not an OAuth-scoped Google service — 401/403 here means "bad
        # api_key", a permanent config error, not "needs re-auth".
        raise classify_exc(exc, "template fetch_items", provider="generic") from exc

    payload = response.json()
    return payload.get("items", []), payload.get("next_cursor")
