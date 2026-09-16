"""POST /api/v1/ai/usage — the external ingest route.

This is how comar-hub, scribe and (eventually) Home Assistant report usage in
without a database dependency of their own — see
`vault/Projects/lios/Plans/AI Broker — Role Registry and Usage Ledger.md`
§4. Nothing in this repo calls it yet; it exists as a contract for those
later callers. Auth and validation are what a contract has to get right on
day one, so that's what this file proves — the actual row-lands-in-the-table
behaviour is `app.services.ai_ledger.record()`'s job, covered in
test_ai_ledger.py / test_ai_usage_model.py.

Follows the `test_http_dispatch_records_transport_and_source_ip` pattern in
test_tool_call_runs.py: a bare FastAPI app carrying just the v1 router, driven
via httpx's ASGI transport.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.db


@pytest.fixture
def tokens(db_session):
    from app.models.clients import ClientToken

    db_session.add(
        ClientToken.for_token(user_id=1, token="alex-client-token", label="test-alex")
    )
    db_session.commit()


def _app():
    from app.api.v1 import router as v1_router

    app = FastAPI()
    app.include_router(v1_router)
    return app


def _post(body: dict, bearer: str | None = None) -> httpx.Response:
    async def _run():
        transport = httpx.ASGITransport(app=_app())
        headers = {"Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/v1/ai/usage", json=body, headers=headers)

    return asyncio.run(_run())


_VALID_BODY = {
    "provider": "anthropic",
    "model": "claude-sonnet-5",
    "kind": "chat",
    "caller": "hub:bridge",
    "units_in": 100,
    "units_out": 50,
    "cost_usd": 0.00105,
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_requires_bearer_token(db_session):
    resp = _post(_VALID_BODY)
    assert resp.status_code == 401


def test_rejects_bogus_token(db_session):
    resp = _post(_VALID_BODY, bearer="not-a-real-token")
    assert resp.status_code == 401


def test_accepts_valid_bearer_token(db_session, tokens):
    resp = _post(_VALID_BODY, bearer="alex-client-token")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_rejects_unknown_kind(db_session, tokens):
    body = dict(_VALID_BODY, kind="not-a-real-kind")
    resp = _post(body, bearer="alex-client-token")
    assert resp.status_code == 422


def test_rejects_missing_required_field(db_session, tokens):
    body = dict(_VALID_BODY)
    del body["provider"]
    resp = _post(body, bearer="alex-client-token")
    assert resp.status_code == 422


@pytest.mark.parametrize("kind", ["chat", "embedding", "stt", "tts", "vision", "prediction"])
def test_accepts_every_valid_kind(db_session, tokens, kind):
    body = dict(_VALID_BODY, kind=kind)
    resp = _post(body, bearer="alex-client-token")
    assert resp.status_code == 200


def test_null_cost_is_accepted_and_distinct_from_zero(db_session, tokens):
    """The route must not coerce a missing/null cost_usd into 0.0."""
    body = dict(_VALID_BODY, cost_usd=None)
    resp = _post(body, bearer="alex-client-token")
    assert resp.status_code == 200

    body_zero = dict(_VALID_BODY, cost_usd=0.0, caller="hub:bridge_local")
    resp_zero = _post(body_zero, bearer="alex-client-token")
    assert resp_zero.status_code == 200


# ---------------------------------------------------------------------------
# End-to-end: the row actually lands via the buffered service
# ---------------------------------------------------------------------------

def test_ingest_lands_a_row(db_session, tokens, monkeypatch):
    from app.models.ai_usage import AiUsage
    from app.services import ai_ledger

    ai_ledger._reset_for_tests()
    try:
        # Land the row via this test's own flush_sync(), not a race against
        # the background thread — see the equivalent note in test_algo_llm.py.
        monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)
        resp = _post(dict(_VALID_BODY, caller="hub:bridge_e2e"), bearer="alex-client-token")
        assert resp.status_code == 200

        ai_ledger.flush_sync()

        row = db_session.query(AiUsage).filter(AiUsage.caller == "hub:bridge_e2e").one_or_none()
        assert row is not None
        assert row.provider == "anthropic"
        assert row.model == "claude-sonnet-5"
        assert row.cost_usd == pytest.approx(0.00105)
    finally:
        ai_ledger._reset_for_tests()
