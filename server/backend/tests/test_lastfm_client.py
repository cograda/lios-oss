"""Tests for Last.fm API client — retry logic, pagination, response parsing.

Loads client.py directly via spec_from_file_location to bypass the
lastfm/__init__.py chain (which imports app.db → fastapi).
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import httpx

# Load client.py directly — it only imports httpx + stdlib
_client_path = Path(__file__).resolve().parent.parent / "app" / "integrations" / "lastfm" / "client.py"
_spec = importlib.util.spec_from_file_location("lastfm_client", str(_client_path))
_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_client)

_parse_track = _client._parse_track
_fetch_with_retry = _client._fetch_with_retry
fetch_all_scrobbles = _client.fetch_all_scrobbles
MAX_RETRIES = _client.MAX_RETRIES


class TestParseTrack:
    def test_normal_track(self):
        track = {
            "name": "Song",
            "artist": {"#text": "Artist"},
            "album": {"#text": "Album"},
            "date": {"uts": "1711900800"},
            "image": [{"#text": ""}, {"#text": "http://img.jpg"}],
            "mbid": "abc-123",
            "loved": "1",
        }
        result = _parse_track(track)
        assert result is not None
        assert result["track_name"] == "Song"
        assert result["artist_name"] == "Artist"
        assert result["album_name"] == "Album"
        assert result["played_at_uts"] == 1711900800
        assert result["album_art_url"] == "http://img.jpg"
        assert result["loved"] is True

    def test_now_playing_returns_none(self):
        track = {
            "name": "Song",
            "artist": {"#text": "Artist"},
            "@attr": {"nowplaying": "true"},
        }
        assert _parse_track(track) is None

    def test_no_date_returns_none(self):
        track = {"name": "Song", "artist": {"#text": "Artist"}}
        assert _parse_track(track) is None

    def test_empty_album(self):
        track = {
            "name": "Song",
            "artist": {"#text": "Artist"},
            "album": {"#text": ""},
            "date": {"uts": "1711900800"},
            "image": [],
        }
        result = _parse_track(track)
        assert result["album_name"] is None
        assert result["album_art_url"] is None


class TestFetchWithRetry:
    @patch.object(_client, "fetch_recent_tracks")
    @patch.object(_client.time, "sleep")
    def test_success_on_first_try(self, mock_sleep, mock_fetch):
        mock_fetch.return_value = {"tracks": [], "pagination": {"page": 1, "total_pages": 1, "total": 0}}
        result = _fetch_with_retry("key", "user", page=1)
        assert result is not None
        mock_sleep.assert_not_called()

    @patch.object(_client, "fetch_recent_tracks")
    @patch.object(_client.time, "sleep")
    def test_retries_on_timeout(self, mock_sleep, mock_fetch):
        mock_fetch.side_effect = [
            httpx.TimeoutException("timeout"),
            {"tracks": [{"x": 1}], "pagination": {"page": 1, "total_pages": 1, "total": 1}},
        ]
        result = _fetch_with_retry("key", "user", page=1)
        assert result is not None
        assert mock_fetch.call_count == 2
        mock_sleep.assert_called_once()

    @patch.object(_client, "fetch_recent_tracks")
    @patch.object(_client.time, "sleep")
    def test_returns_none_after_max_retries(self, mock_sleep, mock_fetch):
        mock_fetch.side_effect = httpx.RequestError("network down")
        result = _fetch_with_retry("key", "user", page=5)
        assert result is None
        assert mock_fetch.call_count == MAX_RETRIES

    @patch.object(_client, "fetch_recent_tracks")
    @patch.object(_client.time, "sleep")
    def test_backoff_increases(self, mock_sleep, mock_fetch):
        mock_fetch.side_effect = httpx.RequestError("fail")
        _fetch_with_retry("key", "user", page=1)
        sleep_values = [call[0][0] for call in mock_sleep.call_args_list]
        assert sleep_values == [2, 5, 15]


class TestFetchAllScrobbles:
    @patch.object(_client, "_fetch_with_retry")
    def test_yields_tracks_with_page_info(self, mock_fetch):
        mock_fetch.side_effect = [
            {"tracks": [{"a": 1}], "pagination": {"page": 1, "total_pages": 2, "total": 2}},
            {"tracks": [{"b": 2}], "pagination": {"page": 2, "total_pages": 2, "total": 2}},
        ]
        results = list(fetch_all_scrobbles("key", "user"))
        assert len(results) == 2
        tracks_1, page_1, total_1 = results[0]
        assert page_1 == 1
        assert total_1 == 2

    @patch.object(_client, "_fetch_with_retry")
    def test_stops_on_retry_exhaustion(self, mock_fetch):
        mock_fetch.side_effect = [
            {"tracks": [{"a": 1}], "pagination": {"page": 1, "total_pages": 5, "total": 1000}},
            None,  # Retry exhausted on page 2
        ]
        results = list(fetch_all_scrobbles("key", "user"))
        assert len(results) == 1

    @patch.object(_client, "_fetch_with_retry")
    def test_start_page_resumes(self, mock_fetch):
        mock_fetch.return_value = {
            "tracks": [{"a": 1}],
            "pagination": {"page": 50, "total_pages": 50, "total": 10000},
        }
        results = list(fetch_all_scrobbles("key", "user", start_page=50))
        assert len(results) == 1
        mock_fetch.assert_called_once_with("key", "user", from_timestamp=None, page=50)
