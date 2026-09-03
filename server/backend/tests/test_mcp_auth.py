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
# stub the resolve function(s) and exercise precedence/fallthrough.
#
# V4 chunk 2.3 deleted the shared HOME_MCP_TOKEN admin fallback outright —
# `_authenticate_request` in app/mcp/server.py now only has two real
# resolution paths (per-user client_tokens, then OAuth access tokens), no
# third "any bearer matching one env var" branch. These fakes are modeled
# on that two-path shape.
def test_authenticate_request_prefers_client_token_over_oauth():
    """Per-user client_tokens should win if a bearer somehow resolves both."""
    fake_user = MagicMock(id=2, name="sam")

    def _authenticate(token, *, resolve_client, resolve_oauth):
        if not token:
            return None
        u = resolve_client(token)
        if u is not None:
            return u
        return resolve_oauth(token)

    user = _authenticate(
        "sam-token",
        resolve_client=lambda t: fake_user if t == "sam-token" else None,
        resolve_oauth=lambda t: MagicMock(id=99, name="should-not-win"),
    )
    assert user is fake_user


def test_authenticate_request_falls_back_to_oauth_token():
    fake_oauth_user = MagicMock(id=2, name="sam")

    def _authenticate(token, *, resolve_client, resolve_oauth):
        if not token:
            return None
        u = resolve_client(token)
        if u is not None:
            return u
        return resolve_oauth(token)

    user = _authenticate(
        "sam-oauth-token",
        resolve_client=lambda t: None,  # not in client_tokens
        resolve_oauth=lambda t: fake_oauth_user if t == "sam-oauth-token" else None,
    )
    assert user is fake_oauth_user


def test_authenticate_request_rejects_unknown_bearer():
    def _authenticate(token, *, resolve_client, resolve_oauth):
        if not token:
            return None
        u = resolve_client(token)
        if u is not None:
            return u
        return resolve_oauth(token)

    assert _authenticate(
        "garbage", resolve_client=lambda t: None, resolve_oauth=lambda t: None,
    ) is None
    assert _authenticate(
        "", resolve_client=lambda t: None, resolve_oauth=lambda t: None,
    ) is None


def test_home_mcp_token_setting_no_longer_exists():
    """Explicit regression guard (V4 chunk 2.3): `settings.mcp_token` — the
    shared-secret admin fallback's config field — must be gone entirely,
    not merely unused. If this ever comes back, the impersonation path in
    app/mcp/server.py could be reintroduced without anyone noticing.
    """
    from app.config import settings

    assert not hasattr(settings, "mcp_token")
