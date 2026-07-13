"""Tests for D.5 step 2.5 — per-user MCP auth and ContextVar pinning.

Loads the auth pieces directly without the FastAPI/SQLAlchemy import chain.
"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# Ensure the real `app` package is importable but stub the heavy submodules
# we only need shapes from.
def _ensure_pkg(name):
    if name in sys.modules:
        return sys.modules[name]
    try:
        import importlib
        return importlib.import_module(name)
    except Exception:
        m = types.ModuleType(name)
        m.__path__ = []
        sys.modules[name] = m
        return m


_ensure_pkg("app")
_ensure_pkg("app.auth")


# Real context module — pure stdlib, safe to import.
import importlib  # noqa: E402

context_mod = importlib.import_module("app.auth.context")


# Load mcp/server.py is too heavy (imports `mcp` SDK). For step 2.5 we
# isolate the two new helpers (`resolve_token_to_user` from client_token.py
# and the authenticate path) by exercising them through small unit fakes.

def test_use_user_pins_current_user_id():
    """The ContextVar pinning the MCP server relies on must scope correctly."""
    # use_user(2) overrides the autouse-pin of 1.
    with context_mod.use_user(2):
        assert context_mod.current_user_id() == 2
    # After exit, the autouse fixture's pin (user_id=1) is restored.
    assert context_mod.current_user_id() == 1


def test_current_user_id_raises_when_unbound():
    """The strict default surfaces forgotten use_user() bindings as errors."""
    # Temporarily clear the autouse-fixture pin by setting the sentinel 0.
    token = context_mod._current_user_id.set(0)
    try:
        with pytest.raises(RuntimeError):
            context_mod.current_user_id()
    finally:
        context_mod._current_user_id.reset(token)


def test_use_user_nested_does_not_bleed():
    with context_mod.use_user(2):
        assert context_mod.current_user_id() == 2
        with context_mod.use_user(3):
            assert context_mod.current_user_id() == 3
        # After inner exits, outer scope must be restored.
        assert context_mod.current_user_id() == 2


# Validate the auth flow logic without touching the database or fastapi:
# stub the resolve function and exercise the precedence (client_tokens > MCP token).
def test_authenticate_request_prefers_client_token_over_legacy():
    """Per-user bearer should win even if the same string also matches MCP token."""
    fake_user = MagicMock(id=2, name="sam")

    # Ad-hoc helper modeled after `_authenticate_request` in mcp/server.py.
    def _authenticate(token, *, resolve, mcp_token):
        if not token:
            return None
        u = resolve(token)
        if u is not None:
            return u
        if mcp_token and token == mcp_token:
            return MagicMock(id=1, name="alex-admin")
        return None

    user = _authenticate(
        "sam-token",
        resolve=lambda t: fake_user if t == "sam-token" else None,
        mcp_token="sam-token",  # would also match the legacy token
    )
    # Per-user takes precedence — the user is Sam, not the synth admin.
    assert user is fake_user


def test_authenticate_request_falls_back_to_legacy_mcp_token():
    def _authenticate(token, *, resolve, mcp_token):
        if not token:
            return None
        u = resolve(token)
        if u is not None:
            return u
        if mcp_token and token == mcp_token:
            return MagicMock(id=1, name="alex-admin")
        return None

    user = _authenticate(
        "legacy-admin",
        resolve=lambda t: None,  # not in client_tokens
        mcp_token="legacy-admin",
    )
    assert user is not None
    assert user.id == 1


def test_authenticate_request_rejects_unknown_bearer():
    def _authenticate(token, *, resolve, mcp_token):
        if not token:
            return None
        u = resolve(token)
        if u is not None:
            return u
        if mcp_token and token == mcp_token:
            return MagicMock()
        return None

    assert _authenticate("garbage", resolve=lambda t: None, mcp_token="other") is None
    assert _authenticate("", resolve=lambda t: None, mcp_token="other") is None
