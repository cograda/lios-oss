"""Shared error hierarchy for the sync/scheduler and tool-dispatch layers.

Scheduler contract (see `app/scheduler.py::_try_sync` / `run_sync`):

- `TransientError` (and any exception that is *not* a `ComarError`, e.g. a
  bare `ConnectionError` or `TimeoutError` bubbling out of an integration's
  `sync()`) is treated as transient: the scheduler retries once after
  `RETRY_DELAY_SECONDS`, then records the failure if the retry also fails.
- `PermanentError` means retrying with the same inputs is pointless — a
  revoked OAuth token, a disabled API, a bad config value. The scheduler
  skips the retry-once step entirely and records the failure immediately.
  Nothing about a `PermanentError` clears itself; the underlying condition
  (re-auth, config fix, etc.) must be resolved out of band before the next
  scheduled sync can succeed.

Integration `sync()` implementations (and the client/http layers they call)
should raise these directly — via `raise TransientError(...) from exc` /
`raise PermanentError(...) from exc` — rather than letting the scheduler
infer intent from exception type or a wrapped `__cause__` chain.
"""


class ComarError(Exception):
    """Base class for all typed comar errors."""


class TransientError(ComarError):
    """A failure that is likely to succeed if retried (network blip, 5xx,
    timeout, rate limit). The scheduler retries once after a short delay."""


class PermanentError(ComarError):
    """A failure that will not resolve itself on retry — bad credentials,
    disabled API, invalid configuration. The scheduler does not retry;
    re-auth or a config change is required before the next sync can succeed.
    """


class NeedsReauthError(PermanentError):
    """Raised when a refresh token is permanently rejected by the provider.

    The token row's `needs_reauth_at` has been set; the scheduler should NOT
    retry the sync — re-running the same dead token costs API quota and
    accumulates consecutive_failures pointlessly. The user must complete a
    fresh OAuth consent flow to recover.

    Carries `account_email` so log lines and SSE events can name the dead
    account without callers having to know the original sync arguments.
    """

    def __init__(self, account_email: str, reason: str):
        self.account_email = account_email
        self.reason = reason
        super().__init__(f"{account_email}: needs re-auth ({reason})")
