"""Unit tests for CanaryLoggingMiddleware (mcp_server.py).

Temporary instrumentation added 2026-07-10 to prove nothing still calls the
legacy localhost MCP proxy before it's deleted. These tests just confirm the
middleware logs non-health requests and stays silent on /health.
"""

import logging

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from comar.mcp_server import CanaryLoggingMiddleware


def _build_app() -> Starlette:
    async def handle_health(request):
        return JSONResponse({"status": "ok"})

    async def handle_mcp(request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/health", endpoint=handle_health),
            Route("/mcp", endpoint=handle_mcp),
        ],
    )
    app.add_middleware(CanaryLoggingMiddleware)
    return app


def test_canary_logs_non_health_request(caplog):
    client = TestClient(_build_app())
    with caplog.at_level(logging.INFO, logger="comar.mcp_server"):
        resp = client.get("/mcp")
    assert resp.status_code == 200
    canary_lines = [r.message for r in caplog.records if "mcp-canary:" in r.message]
    assert len(canary_lines) == 1
    assert "GET" in canary_lines[0]
    assert "/mcp" in canary_lines[0]


def test_canary_silent_on_health(caplog):
    client = TestClient(_build_app())
    with caplog.at_level(logging.INFO, logger="comar.mcp_server"):
        resp = client.get("/health")
    assert resp.status_code == 200
    canary_lines = [r.message for r in caplog.records if "mcp-canary:" in r.message]
    assert canary_lines == []
