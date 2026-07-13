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

# app.config — sync.py reads settings.lastfm_api_key / lastfm_username
_config_mod = ModuleType("app.config")
_config_mod.settings = MagicMock()
_config_mod.settings.lastfm_api_key = "test-key"
_config_mod.settings.lastfm_username = "test-user"
sys.modules.setdefault("app", ModuleType("app"))
sys.modules.setdefault("app.config", _config_mod)

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

# lastfm.client — load the real module (pure httpx, no heavy deps)
_client_path = Path(__file__).resolve().parent.parent / "app" / "integrations" / "lastfm" / "client.py"
_client_spec = importlib.util.spec_from_file_location(
    "app.integrations.lastfm.client", str(_client_path)
)
_client_mod = importlib.util.module_from_spec(_client_spec)
sys.modules["app.integrations.lastfm.client"] = _client_mod
_client_spec.loader.exec_module(_client_mod)

# ---------------------------------------------------------------------------
# Now load sync.py directly
# ---------------------------------------------------------------------------

_sync_path = Path(__file__).resolve().parent.parent / "app" / "integrations" / "lastfm" / "sync.py"
_sync_spec = importlib.util.spec_from_file_location("lastfm_sync", str(_sync_path))
_sync = importlib.util.module_from_spec(_sync_spec)
_sync_spec.loader.exec_module(_sync)

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

        _save_backfill_progress(mock_session, 42, 500, "running")
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
        """Ensure sync.py sees our test settings, not a stale mock."""
        mock_settings = MagicMock()
        mock_settings.lastfm_api_key = "test-key"
        mock_settings.lastfm_username = "test-user"
        with patch.object(_sync, "settings", mock_settings):
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
        backfill_scrobbles(session, resume=True)

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
        backfill_scrobbles(session, resume=False)

        mock_fetch.assert_called_once_with(
            api_key="test-key",
            username="test-user",
            start_page=1,
        )
