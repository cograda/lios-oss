"""Tests for the client auto-updater — version comparison and update flow.

Network happens via httpx since 2.6.1 (the download must carry the per-user
bearer — see `download_and_install`'s docstring), so these tests mock
`comar.updater.httpx`; only the pipx install still goes through subprocess.
"""

import hashlib
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

import httpx

from lios_sync.updater import _fetch_wheel_filename, check_for_update, download_and_install

WHEEL_NAME = "lios_sync-9.9.9-py3-none-any.whl"
WHEEL_BYTES = b"fake wheel content " * 100  # >1KB — passes the size sanity check


def _version_response(wheel: str = WHEEL_NAME) -> MagicMock:
    """Successful GET of /api/client/version."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"version": "9.9.9", "wheel": wheel, "sha256": ""}
    return resp


@contextmanager
def _stream_ok(content: bytes = WHEEL_BYTES):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.iter_bytes.return_value = iter([content])
    yield resp


@contextmanager
def _stream_status(code: int):
    resp = MagicMock()
    request = httpx.Request("GET", "http://10.0.0.1:8400/api/client/download/latest")
    response = httpx.Response(code, request=request)
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=response,
    )
    yield resp


class TestCheckForUpdate:
    def test_newer_version_returns_true(self):
        assert check_for_update("1.0.0", "1.0.1") is True

    def test_same_version_returns_false(self):
        assert check_for_update("1.0.0", "1.0.0") is False

    def test_older_version_returns_false(self):
        assert check_for_update("1.0.1", "1.0.0") is False

    def test_empty_latest_returns_false(self):
        assert check_for_update("1.0.0", "") is False

    def test_empty_current_returns_false(self):
        assert check_for_update("", "1.0.0") is False

    def test_both_empty_returns_false(self):
        assert check_for_update("", "") is False

    def test_major_version_bump(self):
        assert check_for_update("1.9.9", "2.0.0") is True

    def test_patch_version_bump(self):
        assert check_for_update("2.3.0", "2.3.1") is True

    def test_unparseable_versions_compared_as_strings(self):
        assert check_for_update("abc", "def") is True

    def test_unparseable_same_string_returns_false(self):
        assert check_for_update("abc", "abc") is False


class TestFetchWheelFilename:
    @patch("lios_sync.updater.httpx.get")
    def test_returns_server_reported_name(self, mock_get):
        mock_get.return_value = _version_response()
        assert _fetch_wheel_filename("http://10.0.0.1:8400") == WHEEL_NAME

    @patch("lios_sync.updater.httpx.get")
    def test_rejects_path_traversal(self, mock_get):
        mock_get.return_value = _version_response(wheel="../../etc/evil.whl")
        assert _fetch_wheel_filename("http://10.0.0.1:8400") is None

    @patch("lios_sync.updater.httpx.get")
    def test_rejects_non_wheel_name(self, mock_get):
        mock_get.return_value = _version_response(wheel="x.sh")
        assert _fetch_wheel_filename("http://10.0.0.1:8400") is None

    @patch("lios_sync.updater.httpx.get")
    def test_http_failure_returns_none(self, mock_get):
        mock_get.side_effect = httpx.ConnectError("connection refused")
        assert _fetch_wheel_filename("http://10.0.0.1:8400") is None


class TestDownloadAndInstall:
    @patch("lios_sync.updater.httpx.stream")
    @patch("lios_sync.updater.httpx.get")
    def test_download_failure_returns_false(self, mock_get, mock_stream):
        mock_get.return_value = _version_response()
        mock_stream.side_effect = httpx.ConnectError("connection refused")
        assert download_and_install("10.0.0.1:8400") is False

    @patch("lios_sync.updater.httpx.stream")
    @patch("lios_sync.updater.httpx.get")
    def test_401_returns_false(self, mock_get, mock_stream):
        """The gated download endpoint rejecting the bearer must fail the
        update cleanly (this is the exact failure that stranded every daemon
        on 2.5.0 when the updater sent no bearer at all)."""
        mock_get.return_value = _version_response()
        mock_stream.return_value = _stream_status(401)
        assert download_and_install("10.0.0.1:8400", token="stale-token") is False

    @patch("lios_sync.updater.httpx.stream")
    @patch("lios_sync.updater.httpx.get")
    def test_bearer_is_sent(self, mock_get, mock_stream, tmp_path):
        """The download request must carry the per-user bearer — the endpoint
        401s without it (V4 chunk 2.5)."""
        mock_get.return_value = _version_response()
        mock_stream.return_value = _stream_status(401)  # stop after the request
        download_and_install("10.0.0.1:8400", token="sekrit")
        headers = mock_stream.call_args.kwargs["headers"]
        assert headers == {"Authorization": "Bearer sekrit"}

    @patch("lios_sync.updater.httpx.stream")
    @patch("lios_sync.updater.httpx.get")
    def test_downloads_to_real_wheel_filename(self, mock_get, mock_stream, tmp_path):
        """The download must be saved under the server-reported PEP 427
        filename — pipx refuses to install 'comar_client.whl' (it parses
        name/version from the filename). Regressed in production 2026-06-10."""
        mock_get.return_value = _version_response()
        mock_stream.return_value = _stream_ok()
        with patch("lios_sync.updater.tempfile.mkdtemp", return_value=str(tmp_path)), \
             patch("lios_sync.updater.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            download_and_install("10.0.0.1:8400")
        url = mock_stream.call_args[0][1]
        assert url == "http://10.0.0.1:8400/api/client/download/latest"
        # The tmpdir is cleaned up in the function's finally — verify the
        # filename via what the installer was actually asked to install
        # (the wheel path is the last argument for both layouts).
        install_cmd = mock_run.call_args[0][0]
        assert install_cmd[-1].endswith(WHEEL_NAME) or install_cmd[2].endswith(WHEEL_NAME)


class TestChecksumVerification:
    def _patches(self, tmp_path):
        return (
            patch("lios_sync.updater.httpx.get", return_value=_version_response()),
            patch("lios_sync.updater.httpx.stream", return_value=_stream_ok()),
            patch("lios_sync.updater.tempfile.mkdtemp", return_value=str(tmp_path)),
        )

    def test_checksum_mismatch_aborts(self, tmp_path):
        """A wrong checksum should prevent installation."""
        p_get, p_stream, p_tmp = self._patches(tmp_path)
        with p_get, p_stream, p_tmp, \
             patch("lios_sync.updater.subprocess.run") as mock_run:
            result = download_and_install(
                "10.0.0.1:8400",
                expected_checksum="0" * 64,
            )
            assert result is False
            assert mock_run.call_count == 0  # pipx never invoked

    def test_checksum_match_proceeds(self, tmp_path):
        """Correct checksum should allow installation to proceed."""
        correct_checksum = hashlib.sha256(WHEEL_BYTES).hexdigest()
        p_get, p_stream, p_tmp = self._patches(tmp_path)
        with p_get, p_stream, p_tmp, \
             patch("lios_sync.updater.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            result = download_and_install(
                "10.0.0.1:8400",
                expected_checksum=correct_checksum,
            )
            assert result is True
            install_calls = [c for c in mock_run.call_args_list if "install" in c[0][0]]
            assert len(install_calls) == 1

    def test_empty_checksum_skips_verification(self, tmp_path):
        """When server sends no checksum (old server), install proceeds without verification."""
        p_get, p_stream, p_tmp = self._patches(tmp_path)
        with p_get, p_stream, p_tmp, \
             patch("lios_sync.updater.subprocess.run", return_value=MagicMock(returncode=0)):
            result = download_and_install("10.0.0.1:8400", expected_checksum="")
            assert result is True
