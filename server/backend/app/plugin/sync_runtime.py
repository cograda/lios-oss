"""Shared sync runtime: HTTP error classification, multi-account fan-out,
and cursor bookkeeping (V4 chunk 3.2).

Consolidates three patterns every source integration used to hand-roll:

1. HTTP status -> `app.errors` classification. `google_calendar/client.py`,
   `google_mail/client.py`, `sheets/client.py` each carried a byte-for-byte
   copy of `_classify_http_error` (googleapiclient shape); `lastfm/client.py`
   had `_classify_httpx_error` (httpx shape). `classify_http` is the one
   canonical status->type mapping; `classify_exc` adapts a caught exception
   (googleapiclient `HttpError`, httpx errors, bare network errors) into a
   status/retry-after pair and delegates to it.

2. Multi-account fan-out. `google_calendar/__init__.py::sync()` and
   `google_mail/__init__.py::sync()` hand-rolled ~20 lines each: iterate
   accounts, collect successes/failures, and — only when *every* account
   failed — raise an aggregate exception (preserving a lone NeedsReauthError,
   else PermanentError if every failure was permanent, else TransientError).
   A partial failure (some accounts ok, some not) is logged but does NOT
   raise — the sync as a whole is still a success. `fan_out` reproduces this
   exactly; see the two `__init__.py` files' pre-3.2 git history for the
   original hand-rolled version this was lifted from.

3. Incremental bookkeeping. `SyncCursor` is a tiny opt-in helper over the new
   `sync_cursors` table (`app.models.sync_cursor.SyncCursorRow`) for a named
   cursor string per (integration, user, key). Nothing is forced onto it in
   this chunk — existing ad-hoc cursors (e.g. lastfm's backfill page, stashed
   in `SyncState.last_error`) are untouched.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

from sqlalchemy.orm import Session

from app.errors import ComarError, NeedsReauthError, PermanentError, TransientError

logger = logging.getLogger(__name__)

# Providers whose 401/403 *could* mean "needs re-auth" if classify_http is
# given an account_email. In practice google_calendar/google_mail/sheets
# already handle the real dead-refresh-token case upstream (get_credentials
# raises NeedsReauthError before any HTTP call happens) and deliberately
# override 401/403 back to PermanentError at the client layer — see their
# call sites. This default exists for providers that don't have their own
# upstream re-auth detection.
OAUTH_PROVIDERS = {"google"}


def classify_http(
    status: int | None,
    *,
    retry_after: str | float | int | None = None,
    provider: str = "generic",
    context: str = "",
    account_email: str | None = None,
    overrides: dict[int, type[ComarError]] | None = None,
) -> ComarError:
    """Map an HTTP status code to the canonical `app.errors` type.

    - `overrides[status]` wins outright when present — lets a provider remap
      a specific status without subclassing (e.g. google_calendar/google_mail
      keep 401/403 as PermanentError instead of the oauth default below,
      since a dead refresh token is already caught earlier as
      NeedsReauthError by `get_credentials`).
    - 401/403: NeedsReauthError if `provider` is an OAuth provider and an
      `account_email` was given (the caller knows whose token to flag);
      otherwise PermanentError.
    - `status is None` (no HTTP status at all — a network-level failure):
      TransientError.
    - 408, 429, or any 5xx: TransientError. `retry_after` (seconds, or a
      Retry-After header value) is folded into the message when given.
    - Any other 4xx: PermanentError.
    - Anything else unusual (1xx/3xx/6xx+ reaching here): TransientError —
      never silently pass through an unclassified exception.
    """
    label = f"{context}: " if context else ""

    def _msg(extra: str = "") -> str:
        base = f"{label}HTTP {status}" if status is not None else f"{label}network error"
        return f"{base}{extra}"

    if overrides and status in overrides:
        return overrides[status](_msg())

    if status in (401, 403):
        if provider in OAUTH_PROVIDERS and account_email:
            return NeedsReauthError(account_email, f"HTTP {status}")
        return PermanentError(_msg())

    if status is None:
        return TransientError(_msg())

    if status == 408 or status == 429 or status >= 500:
        extra = f" (retry after {retry_after}s)" if retry_after else ""
        return TransientError(_msg(extra))

    if 400 <= status < 500:
        return PermanentError(_msg())

    return TransientError(_msg())


def classify_exc(
    exc: Exception,
    context: str,
    *,
    provider: str = "generic",
    account_email: str | None = None,
    overrides: dict[int, type[ComarError]] | None = None,
) -> ComarError:
    """Classify a caught exception, extracting status/Retry-After where possible.

    Recognizes googleapiclient's `HttpError`, `httpx.HTTPStatusError`, and
    bare network errors (`TimeoutError`, `ConnectionError`, `OSError`, and
    httpx's own timeout/connect/request errors). Anything else falls back to
    `classify_http(None, ...)` (transient) rather than passing the original
    exception through unclassified.
    """
    status: int | None = None
    retry_after: str | None = None

    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
    except ImportError:  # pragma: no cover - always installed in this repo
        GoogleHttpError = ()  # type: ignore[assignment]

    if GoogleHttpError and isinstance(exc, GoogleHttpError):
        status = getattr(exc.resp, "status", None)
        retry_after = getattr(exc.resp, "get", lambda *_: None)("retry-after")
        return classify_http(
            status, retry_after=retry_after, provider=provider,
            context=context, account_email=account_email, overrides=overrides,
        )

    try:
        import httpx
    except ImportError:  # pragma: no cover - always installed in this repo
        httpx = None  # type: ignore[assignment]

    if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retry_after = exc.response.headers.get("retry-after")
        return classify_http(
            status, retry_after=retry_after, provider=provider,
            context=context, account_email=account_email, overrides=overrides,
        )

    if httpx is not None and isinstance(
        exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError)
    ):
        return classify_http(
            None, provider=provider, context=context,
            account_email=account_email, overrides=overrides,
        )

    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return classify_http(
            None, provider=provider, context=context,
            account_email=account_email, overrides=overrides,
        )

    # Unrecognized exception shape — still classify rather than pass through
    # raw, but don't guess a status.
    return classify_http(
        None, provider=provider, context=context,
        account_email=account_email, overrides=overrides,
    )


T = TypeVar("T")
R = TypeVar("R")


@dataclass
class FanOutResult:
    """Aggregate result of `fan_out` over a collection of items."""

    label: str
    succeeded: list[Any] = field(default_factory=list)
    failed: list[tuple[Any, Exception]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.succeeded) + len(self.failed)

    @property
    def ok(self) -> bool:
        """True if nothing failed (all-succeed, including the empty case)."""
        return not self.failed


async def fan_out(
    items: list[T],
    worker: Callable[[T], R] | Callable[[T], Awaitable[R]],
    *,
    label: str,
) -> FanOutResult:
    """Run `worker(item)` for each item, sequentially, aggregating results.

    `worker` may be a plain sync callable (today's usage — every current
    integration's per-account sync function is blocking) or a coroutine
    function; either is awaited/called as appropriate. Sequential execution
    matches current behavior for all four call sites this replaces — nothing
    here parallelizes accounts.

    Exception aggregation reproduces google_calendar/google_mail's exact
    pre-3.2 semantics:

    - Every item failing is the only case that raises. If there was exactly
      one item and its failure was a `NeedsReauthError`, that specific
      exception is re-raised unchanged (preserves the "which account needs
      re-auth" message). Otherwise, if every failure was a `PermanentError`,
      raises `PermanentError`; if any failure was not permanent (transient,
      or a mix), raises `TransientError`. Either way the aggregate message
      lists every failure and the exception is chained (`from` the last one).
    - A partial failure (some succeeded, some didn't) does NOT raise — it's
      logged and returned in `.failed` for the caller to report however it
      likes (today: a log line with the success/total ratio). This matches
      "sync ran, some accounts had trouble" being an overall success.
    - Zero items is not a failure — returns an empty `FanOutResult`.
    """
    result = FanOutResult(label=label)

    for item in items:
        try:
            value = worker(item)
            if inspect.isawaitable(value):
                value = await value
            result.succeeded.append(value)
        except Exception as exc:
            logger.exception(f"{label}: failed for {item!r}")
            result.failed.append((item, exc))

    if items and not result.succeeded:
        last_exc = result.failed[-1][1]
        message = (
            f"All {len(result.failed)} {label} failed: "
            + " | ".join(
                f"{item!r}: {type(exc).__name__}: {exc}"
                for item, exc in result.failed
            )
        )
        if len(result.failed) == 1 and isinstance(last_exc, NeedsReauthError):
            raise last_exc
        if all(isinstance(exc, PermanentError) for _, exc in result.failed):
            raise PermanentError(message) from last_exc
        raise TransientError(message) from last_exc

    if result.failed:
        logger.warning(
            f"{label}: {len(result.failed)}/{result.total} failed: "
            + " | ".join(
                f"{item!r}: {type(exc).__name__}: {exc}"
                for item, exc in result.failed
            )
        )

    return result


class SyncCursor:
    """Get/set a named cursor string per (integration, user, key).

    Backed by the `sync_cursors` table. Opt-in — an integration adopts this
    instead of, e.g., stashing a page number in `SyncState.last_error`
    (lastfm's backfill cursor does that today and is untouched by this
    chunk). `user_id=None` is valid for single-account integrations with no
    natural owning user (weather, lastfm's global sync).
    """

    @staticmethod
    def get(
        session: Session, integration: str, key: str, *, user_id: int | None = None,
    ) -> str | None:
        from app.models.sync_cursor import SyncCursorRow

        row = (
            session.query(SyncCursorRow)
            .filter_by(integration=integration, user_id=user_id, key=key)
            .first()
        )
        return row.value if row else None

    @staticmethod
    def set(
        session: Session,
        integration: str,
        key: str,
        value: str,
        *,
        user_id: int | None = None,
    ) -> None:
        from app.models.sync_cursor import SyncCursorRow

        row = (
            session.query(SyncCursorRow)
            .filter_by(integration=integration, user_id=user_id, key=key)
            .first()
        )
        if row:
            row.value = value
        else:
            session.add(
                SyncCursorRow(
                    integration=integration, user_id=user_id, key=key, value=value,
                )
            )
        session.commit()
