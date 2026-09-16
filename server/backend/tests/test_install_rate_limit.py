"""F-security: `GET /api/install/{code}` had no rate limit at all.

The code is the auth boundary for this route (it's in `AUTH_EXEMPT_PREFIXES`
— a brand-new machine has no bearer yet), which makes a guessed/enumerated
code exactly as sensitive as a guessed bearer token elsewhere on this
boundary. This suite pins the same per-IP failure-budget wiring the bearer
paths already have (`app/auth/rate_limit.py`, see `test_rate_limit.py`):
`is_over_limit()` before any DB lookup, `record_failure()` on every miss
(unknown/redeemed/expired/broken code), never on a genuine hit.

Brute-force budget, for the record: `create_install_code.py` mints codes via
`secrets.token_urlsafe(16)` — 128 bits of entropy over a 64-symbol alphabet.
`MAX_ATTEMPTS_PER_WINDOW` (20) failures per `WINDOW_SECONDS` (10s) per IP
caps a guessing script at roughly 120 attempts/minute against a 2^128 code
space — this fix closes the "literally unlimited" gap, not a
"brute-forceable in practice" one.

Uses hand-built fake `get_db()`/session objects (à la test_rate_limit.py's
fake Request classes) rather than a real Postgres-backed InstallCode row,
since only the rate-limiter wiring is under test here — the route's own
redemption logic (encryption, single-use, expiry) already has its own
coverage elsewhere.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth import rate_limit
from app.routes import install as install_route


@pytest.fixture(autouse=True)
def _clean_limiter():
    rate_limit.reset()
    yield
    rate_limit.reset()


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    def __init__(self, host: str):
        self.client = _FakeClient(host)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Stands in for the SQLAlchemy session the route's `db.session()`
    yields. `execute(...)` always returns whatever InstallCode row (or
    None) this instance was built with; `get(Model, pk)` resolves the two
    lookups the successful path makes (ClientToken, User)."""

    def __init__(self, install_code=None, token=None, user=None):
        self._install_code = install_code
        self._token = token
        self._user = user

    def execute(self, _stmt):
        return _FakeResult(self._install_code)

    def get(self, model, _pk):
        from app.models.clients import ClientToken
        from app.models.users import User

        if model is ClientToken:
            return self._token
        if model is User:
            return self._user
        return None

    def commit(self):
        pass


class _FakeDb:
    def __init__(self, session: _FakeSession):
        self._session = session

    @contextmanager
    def session(self):
        yield self._session


def _exhaust(ip: str) -> None:
    for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW):
        rate_limit.record_failure(ip)


class TestMissesSpendBudget:
    def test_unknown_code_spends_budget_and_eventually_429s(self, monkeypatch):
        db = _FakeDb(_FakeSession(install_code=None))
        monkeypatch.setattr(install_route, "get_db", lambda: db)

        req = _FakeRequest("3.3.3.3")
        for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW):
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(install_route.fetch_install_script("bogus-code", req))
            assert excinfo.value.status_code == 404

        assert rate_limit.is_over_limit("3.3.3.3") is True

    def test_redeemed_code_spends_budget(self, monkeypatch):
        already = SimpleNamespace(
            redeemed_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        db = _FakeDb(_FakeSession(install_code=already))
        monkeypatch.setattr(install_route, "get_db", lambda: db)

        req = _FakeRequest("6.6.6.6")
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(install_route.fetch_install_script("used-code", req))
        assert excinfo.value.status_code == 410

        for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW - 1):
            with pytest.raises(HTTPException):
                asyncio.run(install_route.fetch_install_script("used-code", req))
        assert rate_limit.is_over_limit("6.6.6.6") is True


class TestOverLimitIsRejectedBeforeDbLookup:
    def test_over_limit_ip_gets_429_and_never_touches_the_db(self, monkeypatch):
        _exhaust("4.4.4.4")

        def _boom():
            raise AssertionError("get_db() must not be called once over limit")

        monkeypatch.setattr(install_route, "get_db", _boom)

        req = _FakeRequest("4.4.4.4")
        response = asyncio.run(install_route.fetch_install_script("whatever", req))
        assert response.status_code == 429

    def test_different_ips_have_independent_budgets(self, monkeypatch):
        _exhaust("4.4.4.4")
        db = _FakeDb(_FakeSession(install_code=None))
        monkeypatch.setattr(install_route, "get_db", lambda: db)

        req = _FakeRequest("7.7.7.7")
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(install_route.fetch_install_script("bogus", req))
        assert excinfo.value.status_code == 404  # not 429 — different IP, fresh budget


class TestSuccessfulRedemptionNeverSpendsBudget:
    def test_a_genuine_hit_does_not_count_as_a_failure(self, monkeypatch):
        now = datetime.now(timezone.utc)
        user = SimpleNamespace(id=1, name="alex", is_active=True)
        token = SimpleNamespace(id=10, is_active=True)
        ic = SimpleNamespace(
            id=1,
            code="good-code",
            user_id=1,
            label="test-machine",
            token_id=10,
            token_plaintext="ciphertext",
            created_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=1),
            redeemed_at=None,
            redeemed_from_ip=None,
        )
        db = _FakeDb(_FakeSession(install_code=ic, token=token, user=user))
        monkeypatch.setattr(install_route, "get_db", lambda: db)
        monkeypatch.setattr(install_route, "decrypt_token", lambda _ct: "tok_plaintext")

        req = _FakeRequest("5.5.5.5")
        response = asyncio.run(install_route.fetch_install_script("good-code", req))

        assert response.status_code == 200
        # The failures-only property, same as every other F8 entry point:
        # any volume of successful traffic must never burn the budget.
        for _ in range(rate_limit.MAX_ATTEMPTS_PER_WINDOW * 10):
            assert rate_limit.is_over_limit("5.5.5.5") is False
