"""MCP transport suite (db tier) — real ASGI requests against /mcp.

Drives `mcp_asgi_app` through httpx's ASGITransport: real bearer
resolution against the test Postgres (client_tokens, mcp_access_tokens,
legacy HOME_MCP_TOKEN), real Streamable HTTP framing through the MCP SDK,
real tool dispatch with ContextVar user pinning, the 60s timeout path,
and the 410 tombstone for the removed SSE transport.
"""

import json
import time
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


def _result_text(payload: dict) -> str:
    return payload["result"]["content"][0]["text"]


@pytest.fixture
def mcp_app(real_db, monkeypatch):
    """A fresh Streamable HTTP session manager around the global MCP server,
    with a probe tool that reports the bound user_id.

    Fresh manager per test because the SDK allows exactly one .run() per
    manager instance; fresh probe registration so tests can't couple.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.types import Tool

    from app.mcp import server as srv

    # Ensure the call_tool/list_tools dispatchers are registered (idempotent:
    # re-decoration replaces the handler; duplicate tool defs are guarded).
    if "search_semantic" not in srv._tool_handlers:
        srv._register_embedding_tools()

    def probe_handler(session, arguments):
        from app.auth.context import current_user_id
        if arguments.get("sleep"):
            time.sleep(float(arguments["sleep"]))
        return json.dumps({"user_id": current_user_id()})

    srv._tool_handlers["probe_whoami"] = (probe_handler, "probe")
    probe_def = Tool(
        name="probe_whoami",
        description="test probe",
        inputSchema={"type": "object", "properties": {}},
    )
    srv._tool_definitions.append(probe_def)

    manager = StreamableHTTPSessionManager(
        app=srv.mcp_server, stateless=True, json_response=True,
    )
    monkeypatch.setattr(srv, "session_manager", manager)

    yield srv.mcp_asgi_app

    srv._tool_handlers.pop("probe_whoami", None)
    srv._tool_definitions.remove(probe_def)


@pytest.fixture
def tokens(db_session):
    """One client token per user, one OAuth token for sam, one expired."""
    from app.models.clients import ClientToken
    from app.models.oauth_clients import McpAccessToken

    now = datetime.now(timezone.utc)
    db_session.add_all([
        ClientToken(user_id=1, token="alex-client-token", label="test-alex"),
        ClientToken(user_id=2, token="sam-client-token", label="test-sam"),
        ClientToken(
            user_id=2, token="sam-dead-token", label="revoked", is_active=False,
        ),
        McpAccessToken(
            user_id=2, access_token="sam-oauth-token", client_id="c1",
            expires_at=now + timedelta(hours=1),
        ),
        McpAccessToken(
            user_id=2, access_token="sam-expired-oauth", client_id="c1",
            expires_at=now - timedelta(minutes=1),
        ),
    ])
    db_session.commit()


@pytest.fixture
async def mcp_post(mcp_app):
    """POST helper running inside the session manager's task group."""
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
# Auth paths
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_missing_bearer_is_401(mcp_post, tokens):
    resp = await mcp_post(_rpc("tools/list"))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_invalid_bearer_is_401(mcp_post, tokens):
    resp = await mcp_post(_rpc("tools/list"), bearer="not-a-real-token")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_inactive_client_token_is_401(mcp_post, tokens):
    resp = await mcp_post(_rpc("tools/list"), bearer="sam-dead-token")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_client_token_pins_its_user(mcp_post, tokens):
    resp = await mcp_post(_call("probe_whoami"), bearer="sam-client-token")
    assert resp.status_code == 200
    assert json.loads(_result_text(resp.json())) == {"user_id": 2}


@pytest.mark.anyio
async def test_oauth_token_pins_its_user(mcp_post, tokens):
    resp = await mcp_post(_call("probe_whoami"), bearer="sam-oauth-token")
    assert resp.status_code == 200
    assert json.loads(_result_text(resp.json())) == {"user_id": 2}


@pytest.mark.anyio
async def test_expired_oauth_token_is_401(mcp_post, tokens):
    resp = await mcp_post(_call("probe_whoami"), bearer="sam-expired-oauth")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_legacy_admin_token_pins_alex(mcp_post, tokens, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "mcp_token", "legacy-admin-secret")

    resp = await mcp_post(_call("probe_whoami"), bearer="legacy-admin-secret")
    assert resp.status_code == 200
    assert json.loads(_result_text(resp.json())) == {"user_id": 1}


@pytest.mark.anyio
async def test_legacy_token_disabled_when_unset(mcp_post, tokens, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "mcp_token", "")

    resp = await mcp_post(_call("probe_whoami"), bearer="")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_tools_list_includes_registered_tools(mcp_post, tokens):
    resp = await mcp_post(_rpc("tools/list"), bearer="alex-client-token")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert {"probe_whoami", "search_semantic", "search_stats"} <= names


@pytest.mark.anyio
async def test_unknown_tool_returns_error_payload(mcp_post, tokens):
    resp = await mcp_post(_call("does_not_exist"), bearer="alex-client-token")
    assert resp.status_code == 200
    assert "Unknown tool" in _result_text(resp.json())


@pytest.mark.anyio
async def test_tool_timeout_returns_error_payload(mcp_post, tokens, monkeypatch):
    from app.mcp import server as srv
    monkeypatch.setattr(srv, "MCP_TOOL_TIMEOUT_SECONDS", 0.2)

    resp = await mcp_post(
        _call("probe_whoami", {"sleep": 2}), bearer="alex-client-token"
    )
    assert resp.status_code == 200
    assert "timed out" in _result_text(resp.json())


@pytest.mark.anyio
async def test_handler_exception_returns_error_payload(mcp_post, tokens):
    from app.mcp import server as srv

    def exploding(session, arguments):
        raise ValueError("kaboom")

    srv._tool_handlers["probe_explode"] = (exploding, "probe")
    try:
        resp = await mcp_post(_call("probe_explode"), bearer="alex-client-token")
        assert resp.status_code == 200
        assert "kaboom" in _result_text(resp.json())
    finally:
        srv._tool_handlers.pop("probe_explode", None)


# ---------------------------------------------------------------------------
# Legacy SSE tombstone
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_legacy_sse_path_is_410(mcp_post):
    resp = await mcp_post(_rpc("tools/list"), path="/sse")
    assert resp.status_code == 410
    assert "Streamable HTTP" in resp.json()["error"]


@pytest.mark.anyio
async def test_legacy_messages_path_is_410(mcp_post):
    resp = await mcp_post(_rpc("tools/list"), path="/messages")
    assert resp.status_code == 410
