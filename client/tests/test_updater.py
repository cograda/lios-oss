"""Tests for the client auto-updater — version comparison and update flow."""

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from comar.updater import _fetch_wheel_filename, check_for_update, download_and_install

WHEEL_NAME = "comar_client-9.9.9-py3-none-any.whl"
VERSION_JSON = f'{{"version": "9.9.9", "wheel": "{WHEEL_NAME}", "sha256": ""}}'


def _version_response():
    """Successful curl of /api/client/version."""
    return MagicMock(returncode=0, stdout=VERSION_JSON)


class TestCheckForUpdate:
    def test_newer_version_returns_true(self):
        assert check_for_update("0.1.0", "0.2.0") is True

    def test_same_version_returns_false(self):
        assert check_for_update("0.1.0", "0.1.0") is False

    def test_older_version_returns_false(self):
        assert check_for_update("0.2.0", "0.1.0") is False

    def test_empty_latest_returns_false(self):
        assert check_for_update("0.1.0", "") is False

    def test_empty_current_returns_false(self):
        assert check_for_update("", "0.2.0") is False

    def test_both_empty_returns_false(self):
        assert check_for_update("", "") is False

    def test_major_version_bump(self):
        assert check_for_update("0.9.9", "1.0.0") is True

    def test_patch_version_bump(self):
        assert check_for_update("1.0.0", "1.0.1") is True

    def test_unparseable_versions_compared_as_strings(self):
        # Different strings → True (fallback comparison)
        assert check_for_update("abc", "def") is True

    def test_unparseable_same_string_returns_false(self):
        assert check_for_update("abc", "abc") is False


class TestFetchWheelFilename:
    @patch("comar.updater.subprocess.run")
    def test_returns_server_reported_name(self, mock_run):
        mock_run.return_value = _version_response()
        assert _fetch_wheel_filename("http://10.0.0.1:8400") == WHEEL_NAME

    @patch("comar.updater.subprocess.run")
    def test_rejects_path_traversal(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"wheel": "../../etc/evil.whl"}'
        )
        assert _fetch_wheel_filename("http://10.0.0.1:8400") is None

    @patch("comar.updater.subprocess.run")
    def test_rejects_non_wheel_name(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"wheel": "x.sh"}')
        assert _fetch_wheel_filename("http://10.0.0.1:8400") is None

    @patch("comar.updater.subprocess.run")
    def test_curl_failure_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _fetch_wheel_filename("http://10.0.0.1:8400") is None


class TestDownloadAndInstall:
    @patch("comar.updater.subprocess.run")
    def test_curl_failure_returns_false(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="connection refused")
        result = download_and_install("10.0.0.1:8400", allow_insecure_updates=True)
        assert result is False

    @patch("comar.updater.subprocess.run")
    def test_downloads_to_real_wheel_filename(self, mock_run):
        """The download must be saved under the server-reported PEP 427
        filename — pipx refuses to install 'comar_client.whl' (it parses
        name/version from the filename). Regressed in production 2026-06-10."""
        mock_run.side_effect = [
            _version_response(),
            MagicMock(returncode=1, stderr="stop here"),  # download fails — fine
        ]
        download_and_install("10.0.0.1:8400", allow_insecure_updates=True)

        download_cmd = mock_run.call_args_list[1][0][0]
        assert "http://10.0.0.1:8400/api/client/download/latest" in download_cmd
        target = download_cmd[download_cmd.index("-o") + 1]
        assert target.endswith(WHEEL_NAME)

    @patch("comar.updater.subprocess.run")
    def test_timeout_returns_false(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="curl", timeout=60)
        result = download_and_install("10.0.0.1:8400", allow_insecure_updates=True)
        assert result is False


class TestInsecureUpdateGating:
    """Auto-update must refuse plain http:// unless explicitly opted in —
    checksum verification alone doesn't prove authenticity over a channel an
    on-path attacker can tamper with (both the wheel and its checksum travel
    over the same connection)."""

    @patch("comar.updater.subprocess.run")
    def test_http_blocked_by_default(self, mock_run):
        result = download_and_install("http://10.0.0.1:8400")
        assert result is False
        mock_run.assert_not_called()

    @patch("comar.updater.subprocess.run")
    def test_bare_host_defaults_to_http_and_is_blocked(self, mock_run):
        """No scheme prefix falls back to http:// — must still be gated."""
        result = download_and_install("10.0.0.1:8400")
        assert result is False
        mock_run.assert_not_called()

    @patch("comar.updater.subprocess.run")
    def test_http_allowed_with_explicit_opt_in(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="stop here")
        download_and_install("http://10.0.0.1:8400", allow_insecure_updates=True)
        # Proceeded past the gate — curl was actually invoked.
        mock_run.assert_called()

    @patch("comar.updater.subprocess.run")
    def test_https_never_blocked(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="stop here")
        download_and_install("https://comar.lab")
        mock_run.assert_called()


class TestChecksumVerification:
    def _mock_run(self):
        """subprocess.run mock: version curl returns JSON, others succeed."""
        def run(cmd, **kwargs):
            if "/api/client/version" in " ".join(cmd):
                return _version_response()
            return MagicMock(returncode=0)
        return run

    def test_checksum_mismatch_aborts(self, tmp_path):
        """A wrong checksum should prevent installation."""
        wheel = tmp_path / WHEEL_NAME
        wheel.write_bytes(b"fake wheel content " * 100)  # >1KB

        with patch("comar.updater.subprocess.run", side_effect=self._mock_run()) as mock_run, \
             patch("comar.updater.tempfile.mkdtemp", return_value=str(tmp_path)):
            # download "succeeds" — file already exists at the expected path
            result = download_and_install(
                "10.0.0.1:8400",
                expected_checksum="0000000000000000000000000000000000000000000000000000000000000000",
                allow_insecure_updates=True,
            )
            assert result is False
            # pipx install should never be called (checksum mismatch, not the insecure gate)
            pipx_calls = [c for c in mock_run.call_args_list if "pipx" in str(c)]
            assert len(pipx_calls) == 0
            assert mock_run.call_args_list, "test should exercise the download path, not just the insecure gate"

    def test_checksum_match_proceeds(self, tmp_path):
        """Correct checksum should allow installation to proceed."""
        wheel = tmp_path / WHEEL_NAME
        content = b"fake wheel content " * 100
        wheel.write_bytes(content)
        correct_checksum = hashlib.sha256(content).hexdigest()

        with patch("comar.updater.subprocess.run", side_effect=self._mock_run()) as mock_run, \
             patch("comar.updater.tempfile.mkdtemp", return_value=str(tmp_path)):
            result = download_and_install(
                "10.0.0.1:8400",
                expected_checksum=correct_checksum,
                allow_insecure_updates=True,
            )
            assert result is True
            # pipx install should have been called
            pipx_calls = [c for c in mock_run.call_args_list if "pipx" in str(c[0][0])]
            assert len(pipx_calls) == 1

    def test_empty_checksum_skips_verification(self, tmp_path):
        """When server sends no checksum (old server), install proceeds without verification."""
        wheel = tmp_path / WHEEL_NAME
        wheel.write_bytes(b"fake wheel content " * 100)

        with patch("comar.updater.subprocess.run", side_effect=self._mock_run()), \
             patch("comar.updater.tempfile.mkdtemp", return_value=str(tmp_path)):
            result = download_and_install(
                "10.0.0.1:8400", expected_checksum="", allow_insecure_updates=True,
            )
            assert result is True
