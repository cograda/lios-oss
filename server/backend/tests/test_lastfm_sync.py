"""Tests for Last.fm sync — backfill resume cursor logic.

Loads sync.py directly via spec_from_file_location and pre-mocks heavy
dependencies (SQLAlchemy, app.config, models) in sys.modules so it can
be imported without the full server stack.
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Pre-mock heavy deps that sync.py imports at module level
# ---------------------------------------------------------------------------

# sqlalchemy
_sa_mock = MagicMock()
_sa_mock.func = MagicMock()
sys.modules.setdefault("sqlalchemy", _sa_mock)
sys.modules.setdefault("sqlalchemy.orm", MagicMock())

# app.config — still imported transitively; harmless empty stub.
_config_mod = ModuleType("app.config")
_config_mod.settings = MagicMock()
sys.modules.setdefault("app", ModuleType("app"))
sys.modules.setdefault("app.config", _config_mod)

# app.plugin.config_store — sync.py reads plugin_config("lastfm").lastfm_api_key
# / lastfm_username (V4 chunk 3.3, replaces the old settings.lastfm_* reads).
_plugin_config_mod = ModuleType("app.plugin.config_store")
_plugin_config_mod.plugin_config = MagicMock(
    return_value=MagicMock(lastfm_api_key="test-key", lastfm_username="test-user")
)
sys.modules.setdefault("app.plugin", ModuleType("app.plugin"))
sys.modules.setdefault("app.plugin.config_store", _plugin_config_mod)

# app.models.tokens — SyncState
_tokens_mod = ModuleType("app.models.tokens")
_tokens_mod.SyncState = MagicMock()
sys.modules.setdefault("app.models", ModuleType("app.models"))
sys.modules.setdefault("app.models.tokens", _tokens_mod)

# lastfm subpackage — models and client (sync.py imports from both)
_lastfm_models_mod = ModuleType("app.integrations.lastfm.models")
_lastfm_models_mod.Scrobble = MagicMock()
_lastfm_models_mod.Scrobble.id = "id"
_lastfm_models_mod.ArtistTag = MagicMock()
sys.modules.setdefault("app.integrations", ModuleType("app.integrations"))
sys.modules.setdefault("app.integrations.lastfm", ModuleType("app.integrations.lastfm"))
sys.modules.setdefault("app.integrations.lastfm.models", _lastfm_models_mod)

# lastfm.client — load the real module (pure httpx, no heavy deps).
# The sys.modules entry is scoped to the sync.py load below and restored
# afterwards: a permanent force-assign would shadow the real client module
# for every later-collected test (same pollution class as the old
# test_reminders_commands stub — see improvement-round-2026-07 Chunk H).
_client_path = Path(__file__).resolve().parent.parent / "app" / "integrations" / "lastfm" / "client.py"
_client_spec = importlib.util.spec_from_file_location(
    "app.integrations.lastfm.client", str(_client_path)
)
_client_mod = importlib.util.module_from_spec(_client_spec)
_CLIENT_KEY = "app.integrations.lastfm.client"
_prior_client = sys.modules.get(_CLIENT_KEY)
sys.modules[_CLIENT_KEY] = _client_mod
try:
    _client_spec.loader.exec_module(_client_mod)

    # -----------------------------------------------------------------------
    # Now load sync.py directly (binds the isolated client at exec time)
    # -----------------------------------------------------------------------

    _sync_path = Path(__file__).resolve().parent.parent / "app" / "integrations" / "lastfm" / "sync.py"
    _sync_spec = importlib.util.spec_from_file_location("lastfm_sync", str(_sync_path))
    _sync = importlib.util.module_from_spec(_sync_spec)
    _sync_spec.loader.exec_module(_sync)
finally:
    if _prior_client is not None:
        sys.modules[_CLIENT_KEY] = _prior_client
    else:
        sys.modules.pop(_CLIENT_KEY, None)

_save_backfill_progress = _sync._save_backfill_progress
get_backfill_status = _sync.get_backfill_status
backfill_scrobbles = _sync.backfill_scrobbles


class TestBackfillProgress:
    def test_save_and_read_progress(self, mock_session):
        """Save backfill progress and read it back via get_backfill_status."""
        state = MagicMock()
        state.last_sync_status = "never"
        state.last_error = None
        state.last_sync_at = None
        mock_session.query.return_value.filter_by.return_value.first.return_value = state

        _save_backfill_progress(mock_session, 1, 42, 500, "running")
        assert state.last_sync_status == "running"
        assert state.last_error == "page=42/500"

    def test_get_backfill_status_parses_cursor(self, mock_session):
        state = MagicMock()
        state.last_sync_status = "running"
        state.last_error = "page=150/500"
        state.last_sync_at = datetime(2026, 3, 30, tzinfo=timezone.utc)
        mock_session.query.return_value.filter_by.return_value.first.return_value = state
        mock_session.query.return_value.scalar.return_value = 30000

        result = get_backfill_status(mock_session)
        assert result["status"] == "running"
        assert result["current_page"] == 150
        assert result["total_pages"] == 500
        assert result["total_scrobbles"] == 30000

    def test_get_backfill_status_complete(self, mock_session):
        state = MagicMock()
        state.last_sync_status = "complete"
        state.last_error = "page=500/500"
        state.last_sync_at = datetime(2026, 3, 30, tzinfo=timezone.utc)
        mock_session.query.return_value.filter_by.return_value.first.return_value = state
        mock_session.query.return_value.scalar.return_value = 50000

        result = get_backfill_status(mock_session)
        assert result["status"] == "complete"


class TestBackfillResume:
    @pytest.fixture(autouse=True)
    def _patch_settings(self):
        """Ensure sync.py sees our test config, not a stale mock.

        `resolve_accounts` is patched too: since Last.fm went multi-user,
        `backfill_scrobbles` resolves which (user, username) pair it is
        backfilling, and that reads config from the DB-backed store.
        """
        import app.integrations.lastfm as _pkg

        mock_cfg = MagicMock()
        mock_cfg.lastfm_api_key = "test-key"
        mock_cfg.lastfm_username = "test-user"
        account = _pkg.LastfmAccount(user_id=1, user_name="tester", username="test-user")
        with (
            patch.object(_sync, "plugin_config", return_value=mock_cfg),
            patch.object(_pkg, "resolve_accounts", return_value=[account]),
        ):
            yield

    @patch.object(_sync, "fetch_all_scrobbles")
    @patch.object(_sync, "_upsert_scrobbles", return_value=5)
    @patch.object(_sync, "_save_backfill_progress")
    @patch.object(_sync, "_get_backfill_state")
    def test_resume_reads_cursor(self, mock_get_state, mock_save, mock_upsert, mock_fetch):
        """When resume=True and cursor exists, start from saved page."""
        state = MagicMock()
        state.last_sync_status = "interrupted"
        state.last_error = "page=100/500"
        mock_get_state.return_value = state

        mock_fetch.return_value = iter([])

        session = MagicMock()
        session.query.return_value.scalar.return_value = 50000
        backfill_scrobbles(session, resume=True, user_id=1)

        mock_fetch.assert_called_once_with(
            api_key="test-key",
            username="test-user",
            start_page=100,
        )

    @patch.object(_sync, "fetch_all_scrobbles")
    @patch.object(_sync, "_upsert_scrobbles", return_value=0)
    @patch.object(_sync, "_save_backfill_progress")
    @patch.object(_sync, "_get_backfill_state")
    def test_no_resume_starts_from_page_1(self, mock_get_state, mock_save, mock_upsert, mock_fetch):
        state = MagicMock()
        state.last_sync_status = "interrupted"
        state.last_error = "page=100/500"
        mock_get_state.return_value = state

        mock_fetch.return_value = iter([])
        session = MagicMock()
        session.query.return_value.scalar.return_value = 0
        backfill_scrobbles(session, resume=False, user_id=1)

        mock_fetch.assert_called_once_with(
            api_key="test-key",
            username="test-user",
            start_page=1,
        )
