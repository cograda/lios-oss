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

    from app.mcp import server as srv

    if "search_semantic" not in srv._tool_handlers:
        srv._register_embedding_tools()

    manager = StreamableHTTPSessionManager(
        app=srv.mcp_server, stateless=True, json_response=True,
    )
    monkeypatch.setattr(srv, "session_manager", manager)

    yield srv.mcp_asgi_app


@pytest.fixture
def tokens(db_session):
    from app.models.clients import ClientToken

    db_session.add(ClientToken(user_id=1, token="alex-client-token", label="test-alex"))
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
