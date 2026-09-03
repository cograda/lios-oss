"""SQLAlchemy models.

V4 chunk 1.2: kernel-owned models stay explicit below. Integration models
are discovered from each integration's `manifest.py` (`models: list[str]`)
via `app.plugin.discovery.discover_integration_models()` — adding a new
integration's models to schema/migration discovery requires editing only
its own `manifest.py`, never this file.
"""

from app.models.users import User
from app.models.tokens import OAuthToken, SyncState, SyncHistory
from app.models.clients import ClientToken, ClientLog, InstallCode
from app.models.tool_calls import ToolCall
from app.models.auth_events import AuthEvent
from app.models.oauth_clients import (
    OAuthClient,
    OAuthLoginSession,
    OAuthAuthorizationCode,
    McpAccessToken,
)

# `Embedding`/`EmbeddingQueue` used to be imported explicitly here too (they
# used to live in app.services.embedding, a module with no manifest of its
# own). V4 chunk 3.4 moved them into app.integrations.embedding.models and
# gave that package a real manifest declaring them — they're now picked up
# by the manifest-driven discover_integration_models() sweep below, same as
# every other integration's models.

# `sheets` used to be a manifest-less library (its one model, SheetExport,
# imported explicitly here). V4 chunk 4.2 turned it into a real
# CapabilityService with its own manifest declaring `models=["SheetExport"]`
# — it's now picked up by the manifest-driven discover_integration_models()
# sweep below, same as every other integration's models.
from app.models.sync_cursor import SyncCursorRow
from app.models.integration_config import IntegrationConfig
from app.models.user_preferences import UserPreference
# Cross-user read grants. Kernel-owned rather than obsidian-owned: the check
# runs in `app/plugin/dispatch.py` for every integration, and the scope
# allowlist has to be readable without importing an integration package.
from app.models.vault_grants import VaultReadGrant
# Shared algo-harness tables (predictions, model versions, runs). Kernel-owned
# on purpose — see app/models/algo.py's docstring: the manifest requires an
# integration's models to live in its own models.py, so per-integration
# ownership would mean one predictions table per algo and per-algo scoring
# code. Adding a deriver still touches zero kernel files.
from app.models.algo import AlgoModelVersion, AlgoPrediction, AlgoRun

# The AI usage ledger (lios W2 chunk 1). Kernel-owned for the same reason as
# the algo-harness tables above — it has to be readable across every
# integration, comar-hub, and scribe at once. Written only through
# app.services.ai_ledger, never directly. See app/models/ai_usage.py.
from app.models.ai_usage import AiUsage

from app.plugin.discovery import discover_integration_models

_KERNEL_OWNED = [
    "User",
    "OAuthToken",
    "SyncState",
    "SyncHistory",
    "ClientToken",
    "ClientLog",
    "InstallCode",
    "ToolCall",
    "AuthEvent",
    "OAuthClient",
    "OAuthLoginSession",
    "OAuthAuthorizationCode",
    "McpAccessToken",
    "SyncCursorRow",
    "IntegrationConfig",
    "UserPreference",
    "AlgoModelVersion",
    "AlgoPrediction",
    "AlgoRun",
    "AiUsage",
    "VaultReadGrant",
]

# Import every integration's declared models (manifest.models) into this
# module's namespace, in manifest-discovery order (sorted by integration
# name — deterministic, see discover_integration_models()), so
# `create_tables()`/Alembic see every ORM class exactly as before.
_integration_models = discover_integration_models()
globals().update(_integration_models)

__all__ = _KERNEL_OWNED + sorted(_integration_models.keys())
