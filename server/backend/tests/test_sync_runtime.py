"""Unit tests for app.plugin.sync_runtime (V4 chunk 3.2).

Covers the two pure pieces the chunk brief calls out explicitly: the
classify_http status->type matrix (incl. Retry-After) and fan_out's
all-succeed / all-fail / mixed aggregation semantics (which must match
google_calendar/google_mail's pre-3.2 hand-rolled behavior exactly — see
sync_runtime.py's fan_out docstring).
"""

import asyncio

import pytest

from app.errors import NeedsReauthError, PermanentError, TransientError
from app.plugin.sync_runtime import FanOutResult, SyncCursor, classify_http, fan_out


# --- classify_http ----------------------------------------------------------


def test_401_generic_provider_is_permanent():
    err = classify_http(401, provider="generic")
    assert isinstance(err, PermanentError)
    assert not isinstance(err, NeedsReauthError)


def test_403_generic_provider_is_permanent():
    assert isinstance(classify_http(403, provider="generic"), PermanentError)


def test_401_oauth_provider_with_account_is_needs_reauth():
    err = classify_http(401, provider="google", account_email="alex@example.com")
    assert isinstance(err, NeedsReauthError)
    assert err.account_email == "alex@example.com"


def test_401_oauth_provider_without_account_falls_back_to_permanent():
    err = classify_http(401, provider="google", account_email=None)
    assert isinstance(err, PermanentError)
    assert not isinstance(err, NeedsReauthError)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_retryable_statuses_are_transient(status):
    err = classify_http(status, provider="generic")
    assert isinstance(err, TransientError)


def test_retry_after_is_folded_into_message():
    err = classify_http(429, retry_after=30, provider="generic")
    assert isinstance(err, TransientError)
    assert "30" in str(err)


def test_retry_after_absent_produces_clean_message():
    err = classify_http(429, provider="generic")
    assert "retry after" not in str(err)


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_other_4xx_is_permanent(status):
    assert isinstance(classify_http(status, provider="generic"), PermanentError)


def test_no_status_network_error_is_transient():
    assert isinstance(classify_http(None, provider="generic"), TransientError)


def test_overrides_win_over_default_mapping():
    err = classify_http(401, provider="google", account_email="a@b.com",
                         overrides={401: PermanentError})
    assert isinstance(err, PermanentError)
    assert not isinstance(err, NeedsReauthError)


def test_unusual_status_falls_back_to_transient():
    # e.g. a 3xx or 6xx reaching this point (shouldn't happen, but never
    # silently pass through unclassified)
    assert isinstance(classify_http(999, provider="generic"), TransientError)


# --- fan_out ------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_fan_out_all_succeed():
    result = _run(fan_out([1, 2, 3], lambda x: x * 2, label="things"))
    assert isinstance(result, FanOutResult)
    assert result.succeeded == [2, 4, 6]
    assert result.failed == []
    assert result.ok


def test_fan_out_empty_items_does_not_raise():
    result = _run(fan_out([], lambda x: x, label="things"))
    assert result.succeeded == []
    assert result.failed == []
    assert result.ok


def test_fan_out_all_fail_permanent_raises_permanent():
    def worker(item):
        raise PermanentError(f"bad {item}")

    with pytest.raises(PermanentError):
        _run(fan_out(["a", "b"], worker, label="accounts"))


def test_fan_out_all_fail_transient_raises_transient():
    def worker(item):
        raise TransientError(f"bad {item}")

    with pytest.raises(TransientError):
        _run(fan_out(["a", "b"], worker, label="accounts"))


def test_fan_out_all_fail_mixed_permanent_and_transient_raises_transient():
    def worker(item):
        if item == "a":
            raise PermanentError("dead")
        raise TransientError("blip")

    with pytest.raises(TransientError):
        _run(fan_out(["a", "b"], worker, label="accounts"))


def test_fan_out_single_needs_reauth_failure_reraises_it_unchanged():
    def worker(item):
        raise NeedsReauthError(item, "token revoked")

    with pytest.raises(NeedsReauthError) as exc_info:
        _run(fan_out(["alex@example.com"], worker, label="accounts"))
    assert exc_info.value.account_email == "alex@example.com"


def test_fan_out_multiple_failures_including_needs_reauth_does_not_reraise_bare():
    """A lone NeedsReauthError is only preserved verbatim when it's the ONLY
    failure — matches google_calendar/__init__.py's `len(failure_excs) == 1`
    guard exactly."""
    def worker(item):
        if item == "a":
            raise NeedsReauthError(item, "token revoked")
        raise PermanentError("also dead")

    with pytest.raises(PermanentError) as exc_info:
        _run(fan_out(["a", "b"], worker, label="accounts"))
    assert not isinstance(exc_info.value, NeedsReauthError)


def test_fan_out_mixed_success_and_failure_does_not_raise():
    def worker(item):
        if item == "bad":
            raise TransientError("nope")
        return item

    result = _run(fan_out(["ok1", "bad", "ok2"], worker, label="accounts"))
    assert result.succeeded == ["ok1", "ok2"]
    assert len(result.failed) == 1
    assert result.failed[0][0] == "bad"
    assert not result.ok


def test_fan_out_accepts_async_worker():
    async def worker(item):
        return item + 1

    result = _run(fan_out([1, 2], worker, label="things"))
    assert result.succeeded == [2, 3]


# --- SyncCursor -----------------------------------------------------------


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self._row


class _FakeSession:
    """Minimal session stub — enough for SyncCursor.get/set's query shape."""

    def __init__(self, existing_row=None):
        self._existing_row = existing_row
        self.added = []
        self.committed = False

    def query(self, model):
        return _FakeQuery(self._existing_row)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True


def test_sync_cursor_get_returns_none_when_absent():
    session = _FakeSession(existing_row=None)
    assert SyncCursor.get(session, "lastfm", "backfill_page") is None


def test_sync_cursor_get_returns_existing_value():
    from types import SimpleNamespace

    row = SimpleNamespace(value="42")
    session = _FakeSession(existing_row=row)
    assert SyncCursor.get(session, "lastfm", "backfill_page") == "42"


def test_sync_cursor_set_inserts_when_absent():
    session = _FakeSession(existing_row=None)
    SyncCursor.set(session, "lastfm", "backfill_page", "7", user_id=None)
    assert len(session.added) == 1
    assert session.added[0].value == "7"
    assert session.committed


def test_sync_cursor_set_updates_existing_row_in_place():
    from types import SimpleNamespace

    row = SimpleNamespace(value="old")
    session = _FakeSession(existing_row=row)
    SyncCursor.set(session, "lastfm", "backfill_page", "new", user_id=None)
    assert row.value == "new"
    assert session.added == []
    assert session.committed
