"""tool_calls audit trail (db tier).

Covers the three DB-backed guarantees Phase 5 adds on top of the existing
MCP dispatch path (see tests/test_mcp_transport.py for the auth/dispatch
suite this borrows fixtures from):

  (a) dispatching a real tool through the MCP call_tool path lands exactly
      one `tool_calls` row with a sane duration/status
  (b) >3 error-status rows for one tool name in the last hour trips the
      "failing repeatedly" system_alerts check
  (c) a p95 duration over 5000ms (>=10 calls, 24h window) trips the
      "p95 slow" system_alerts check
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.db


@pytest.fixture
def anyio_backend():
    return "asyncio"


HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _rpc(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def _call(name: str, arguments: dict | None = None) -> dict:
    return _rpc("tools/call", {"name": name, "arguments": arguments or {}})


@pytest.fixture
def mcp_app(real_db, monkeypatch):
    """Fresh Streamable HTTP session manager around the global MCP server.

    Mirrors tests/test_mcp_transport.py's `mcp_app` fixture — a fresh
    manager per test since the SDK allows exactly one .run() per instance.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from app.integrations import register_all
    from app.mcp import server as srv

    # V4 chunk 3.4: embedding's tools are registered through the normal
    # per-integration loop now (embedding is integration #20), not a
    # standalone `_register_embedding_tools()` — guard stays so this only
    # runs once across the test session (module-level registries persist
    # across tests).
    if "search_semantic" not in srv._tool_handlers:
        register_all()
        srv.register_mcp_tools()

    manager = StreamableHTTPSessionManager(
        app=srv.mcp_server, stateless=True, json_response=True,
    )
    monkeypatch.setattr(srv, "session_manager", manager)

    yield srv.mcp_asgi_app


@pytest.fixture
def tokens(db_session):
    from app.models.clients import ClientToken

    db_session.add(
        ClientToken.for_token(user_id=1, token="alex-client-token", label="test-alex")
    )
    db_session.commit()


@pytest.fixture
async def mcp_post(mcp_app):
    from app.mcp import server as srv

    async with srv.session_manager.run():
        async def post(body, bearer=None, path="/"):
            headers = dict(HEADERS)
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            transport = httpx.ASGITransport(app=mcp_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.post(path, json=body, headers=headers)

        yield post


# ---------------------------------------------------------------------------
# (a) dispatching a tool lands exactly one tool_calls row
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_dispatch_records_one_tool_calls_row(mcp_post, tokens, db_session):
    from app.models.tool_calls import ToolCall

    resp = await mcp_post(_call("search_stats"), bearer="alex-client-token")
    assert resp.status_code == 200

    rows = db_session.query(ToolCall).filter(ToolCall.name == "search_stats").all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "ok"
    assert row.user_id == 1
    assert row.duration_ms >= 0
    assert row.tool_call_id and len(row.tool_call_id) == 8  # secrets.token_hex(4)
    assert row.error is None
    assert row.called_at is not None


# ---------------------------------------------------------------------------
# V4 chunk 2.5: audit columns — args_summary/transport/source_ip on both
# transports, and the optional `affected` field for opted-in write tools.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_dispatch_records_transport_and_args_summary(mcp_post, tokens, db_session):
    import json

    from app.models.tool_calls import ToolCall

    resp = await mcp_post(
        _call("search_stats", {"probe_field": "probe-value"}), bearer="alex-client-token",
    )
    assert resp.status_code == 200

    row = (
        db_session.query(ToolCall)
        .filter(ToolCall.name == "search_stats")
        .order_by(ToolCall.id.desc())
        .first()
    )
    assert row is not None
    assert row.transport == "mcp"
    # httpx ASGITransport doesn't set a real client address; source_ip may
    # be None in-process — the important thing is the column exists and the
    # transport value made it through end to end.
    assert row.args_summary is not None
    parsed = json.loads(row.args_summary)
    assert parsed.get("probe_field") == "probe-value"


def test_http_dispatch_records_transport_and_source_ip(db_session, monkeypatch, tokens):
    """Drives app.api.v1.call_tool directly via httpx ASGITransport against
    a minimal FastAPI app carrying just the v1 router — proves the HTTP
    adapter (not just MCP) threads transport/source_ip into dispatch_tool."""
    import asyncio

    import httpx
    from fastapi import FastAPI

    from app.api.v1 import router as v1_router
    from app.integrations import register_all
    from app.mcp import server as srv
    from app.models.tool_calls import ToolCall

    # This test builds its own bare FastAPI app rather than using the
    # `mcp_app` fixture above, so it can't rely on that fixture's
    # registration guard having already run — do it directly rather than
    # depending on other tests in this file having executed first.
    if "search_stats" not in srv._tool_handlers:
        register_all()
        srv.register_mcp_tools()

    app = FastAPI()
    app.include_router(v1_router)

    async def _run():
        transport = httpx.ASGITransport(app=app, client=("192.0.2.42", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/v1/tools/search_stats",
                json={},
                headers={
                    "Authorization": "Bearer alex-client-token",
                    "Content-Type": "application/json",
                },
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200

    row = (
        db_session.query(ToolCall)
        .filter(ToolCall.name == "search_stats")
        .order_by(ToolCall.id.desc())
        .first()
    )
    assert row is not None
    assert row.transport == "http"
    assert row.source_ip == "192.0.2.42"


@pytest.mark.anyio
async def test_snag_add_records_affected(mcp_post, tokens, db_session, tmp_path, monkeypatch):
    from app.config import settings
    from app.models.tool_calls import ToolCall

    # snag_add renders the vault note as part of the write (real behavior,
    # not something to stub out) — give it a real directory to write into,
    # matching a production deployment's actual vault mount, instead of the
    # default /vaults which doesn't exist on a test runner.
    (tmp_path / "alex").mkdir()
    monkeypatch.setattr(settings, "vaults_root_path", str(tmp_path))

    resp = await mcp_post(
        _call("snag_add", {"title": "Cracked tile", "room": "Kitchen"}),
        bearer="alex-client-token",
    )
    assert resp.status_code == 200

    row = (
        db_session.query(ToolCall)
        .filter(ToolCall.name == "snag_add")
        .order_by(ToolCall.id.desc())
        .first()
    )
    assert row is not None
    assert row.affected is not None
    affected = json.loads(row.affected)
    assert len(affected) == 1
    assert affected[0].startswith("snag:")


@pytest.mark.anyio
async def test_tool_without_affected_leaves_it_null(mcp_post, tokens, db_session):
    from app.models.tool_calls import ToolCall

    resp = await mcp_post(_call("search_stats"), bearer="alex-client-token")
    assert resp.status_code == 200

    row = (
        db_session.query(ToolCall)
        .filter(ToolCall.name == "search_stats")
        .order_by(ToolCall.id.desc())
        .first()
    )
    assert row is not None
    assert row.affected is None


# ---------------------------------------------------------------------------
# auth_events — a 401 (either transport) writes a row.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_401_writes_auth_events_row(mcp_post, db_session):
    from app.models.auth_events import AuthEvent

    resp = await mcp_post(_call("search_stats"), bearer="not-a-real-token")
    assert resp.status_code == 401

    rows = db_session.query(AuthEvent).filter(AuthEvent.outcome == "401").all()
    assert len(rows) >= 1
    assert any(r.transport == "mcp" for r in rows)


def test_http_401_writes_auth_events_row(db_session):
    import asyncio

    import httpx
    from fastapi import FastAPI

    from app.api.v1 import router as v1_router
    from app.models.auth_events import AuthEvent

    app = FastAPI()
    app.include_router(v1_router)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                "/api/v1/heartbeat", headers={"Authorization": "Bearer bogus-token"},
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 401

    rows = db_session.query(AuthEvent).filter(AuthEvent.outcome == "401").all()
    assert len(rows) >= 1
    assert any(r.transport == "http" for r in rows)


@pytest.mark.anyio
async def test_dispatch_error_records_error_status(mcp_post, tokens, db_session):
    from app.mcp import server as srv
    from app.models.tool_calls import ToolCall

    def exploding(session, arguments):
        raise ValueError("kaboom")

    srv._tool_handlers["probe_tool_calls_explode"] = (exploding, "probe")
    from mcp.types import Tool
    probe_def = Tool(
        name="probe_tool_calls_explode", description="test probe",
        inputSchema={"type": "object", "properties": {}},
    )
    srv._tool_definitions.append(probe_def)
    try:
        resp = await mcp_post(_call("probe_tool_calls_explode"), bearer="alex-client-token")
        assert resp.status_code == 200

        rows = db_session.query(ToolCall).filter(
            ToolCall.name == "probe_tool_calls_explode"
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "error"
        assert "kaboom" in (rows[0].error or "")
    finally:
        srv._tool_handlers.pop("probe_tool_calls_explode", None)
        srv._tool_definitions.remove(probe_def)


# ---------------------------------------------------------------------------
# (b) + (c) system_alerts tool-call health checks
# ---------------------------------------------------------------------------

def _seed_tool_call(session, *, name, status, duration_ms, called_at, user_id=1):
    from app.models.tool_calls import ToolCall

    session.add(ToolCall(
        tool_call_id="seed0001",
        name=name,
        user_id=user_id,
        duration_ms=duration_ms,
        status=status,
        error="seeded failure" if status == "error" else None,
        called_at=called_at,
    ))


def test_repeated_failures_trigger_alert(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    for _ in range(4):  # > TOOL_FAILURE_THRESHOLD (3)
        _seed_tool_call(
            db_session, name="flaky_tool", status="error",
            duration_ms=50, called_at=now - timedelta(minutes=5),
        )
    db_session.commit()

    payload = json.loads(handle_alerts(db_session, {}))
    tool_alerts = payload["tool_alerts"]
    assert any(
        a["tool"] == "flaky_tool" and "failing repeatedly" in a["issue"]
        for a in tool_alerts
    )
    assert payload["status"] == "degraded"


def test_few_failures_do_not_trigger_alert(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    for _ in range(2):  # below TOOL_FAILURE_THRESHOLD (3)
        _seed_tool_call(
            db_session, name="mostly_fine_tool", status="error",
            duration_ms=50, called_at=now - timedelta(minutes=5),
        )
    db_session.commit()

    payload = json.loads(handle_alerts(db_session, {}))
    assert not any(a["tool"] == "mostly_fine_tool" for a in payload["tool_alerts"])


def test_p95_slow_tool_triggers_alert(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    # 10 calls, all comfortably over the 5000ms threshold so p95 is too.
    for _ in range(10):
        _seed_tool_call(
            db_session, name="slow_tool", status="ok",
            duration_ms=8000, called_at=now - timedelta(hours=1),
        )
    db_session.commit()

    payload = json.loads(handle_alerts(db_session, {}))
    tool_alerts = payload["tool_alerts"]
    assert any(
        a["tool"] == "slow_tool" and "p95 duration" in a["issue"]
        for a in tool_alerts
    )
    assert payload["status"] == "degraded"


def test_p95_ignores_tools_with_too_few_calls(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    # Only 5 calls — below TOOL_P95_MIN_CALLS (10), even though all are slow.
    for _ in range(5):
        _seed_tool_call(
            db_session, name="rarely_used_slow_tool", status="ok",
            duration_ms=9000, called_at=now - timedelta(hours=1),
        )
    db_session.commit()

    payload = json.loads(handle_alerts(db_session, {}))
    assert not any(a["tool"] == "rarely_used_slow_tool" for a in payload["tool_alerts"])


def test_p95_ignores_calls_outside_24h_window(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    for _ in range(10):
        _seed_tool_call(
            db_session, name="old_slow_tool", status="ok",
            duration_ms=9000, called_at=now - timedelta(hours=48),
        )
    db_session.commit()

    payload = json.loads(handle_alerts(db_session, {}))
    assert not any(a["tool"] == "old_slow_tool" for a in payload["tool_alerts"])


def test_a_long_transport_label_is_truncated_not_dropped(db_session):
    """2026-09-02: a script dispatched ~60 tool calls with
    transport="script:backlog-review". The column is 10 chars; every audit
    insert failed and was swallowed, so the calls ran unrecorded. A label
    that does not fit is shortened; the row is never the thing that gives."""
    from app.models.tool_calls import ToolCall
    from app.services.tool_calls import record_tool_call

    record_tool_call(
        name="tasks_query", user_id=None, duration_ms=1, status="ok", error=None,
        tool_call_id="tc-truncate", transport="script:backlog-review",
    )
    row = db_session.query(ToolCall).filter_by(tool_call_id="tc-truncate").one()
    assert row.transport == "script:bac"
