"""Tests for client distribution endpoints — wheel discovery, version extraction.

Pure logic tests (1-5) need no imports. Tests 6-7 load client_dist.py
directly via spec_from_file_location with pre-mocked FastAPI/app.config.
"""

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch, MagicMock

import pytest


class TestWheelDiscovery:
    """Test the wheel file scanning logic without needing FastAPI."""

    def test_find_latest_wheel(self, tmp_path):
        """Newest wheel (by filename sort) should be found."""
        (tmp_path / "comar_client-0.1.0-py3-none-any.whl").touch()
        (tmp_path / "comar_client-0.2.0-py3-none-any.whl").touch()

        wheels = sorted(tmp_path.glob("comar_client-*.whl"), reverse=True)
        assert len(wheels) == 2
        assert "0.2.0" in wheels[0].name

    def test_version_extraction_from_wheel_name(self):
        """Regex should extract version from standard wheel filename."""
        name = "comar_client-1.2.3-py3-none-any.whl"
        m = re.search(r"comar_client-([^-]+)-", name)
        assert m is not None
        assert m.group(1) == "1.2.3"

    def test_version_extraction_with_prerelease(self):
        name = "comar_client-1.2.3rc1-py3-none-any.whl"
        m = re.search(r"comar_client-([^-]+)-", name)
        assert m is not None
        assert m.group(1) == "1.2.3rc1"

    def test_no_wheels_returns_none(self, tmp_path):
        wheels = sorted(tmp_path.glob("comar_client-*.whl"), reverse=True)
        assert len(wheels) == 0

    def test_filename_validation_regex(self):
        """The download endpoint validates filenames — test the pattern."""
        pattern = r"^comar_client-[\w.]+-[\w.]+-[\w.]+-[\w.]+\.whl$"
        assert re.match(pattern, "comar_client-0.1.0-py3-none-any.whl")
        assert not re.match(pattern, "../../../etc/passwd")
        assert not re.match(pattern, "malicious.whl")
        assert not re.match(pattern, "comar_client-0.1.0-py3-none-any.tar.gz")


# ---------------------------------------------------------------------------
# Tests that need the actual client_dist module
# ---------------------------------------------------------------------------

# Pre-mock FastAPI and app.config before loading client_dist.py
sys.modules.setdefault("fastapi", MagicMock())
sys.modules.setdefault("fastapi.responses", MagicMock())
sys.modules.setdefault("app", ModuleType("app"))

_config_mod = ModuleType("app.config")
_config_mod.settings = MagicMock()
_config_mod.settings.server_public_url = "https://comar.lab"
sys.modules.setdefault("app.config", _config_mod)

_dist_path = Path(__file__).resolve().parent.parent / "app" / "routes" / "client_dist.py"
try:
    _spec = importlib.util.spec_from_file_location("client_dist", str(_dist_path))
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _HAS_CLIENT_DIST = True
except Exception:
    _HAS_CLIENT_DIST = False
    _mod = None


@pytest.mark.skipif(not _HAS_CLIENT_DIST, reason="Cannot load client_dist (missing dep)")
class TestClientDistModule:
    def test_no_dist_dir_returns_none(self):
        """If no distribution directory exists, _find_dist_dir returns None."""
        with patch.object(_mod, "_DIST_DIRS", [Path("/nonexistent/a"), Path("/nonexistent/b")]):
            assert _mod._find_dist_dir() is None

    def test_get_latest_wheel_with_dist_dir(self, tmp_path):
        """_get_latest_wheel finds the newest wheel in a real directory."""
        (tmp_path / "comar_client-0.1.0-py3-none-any.whl").write_bytes(b"old")
        (tmp_path / "comar_client-0.3.0-py3-none-any.whl").write_bytes(b"new")
        (tmp_path / "unrelated-1.0.0.whl").touch()

        with patch.object(_mod, "_find_dist_dir", return_value=tmp_path):
            result = _mod._get_latest_wheel()
            assert result is not None
            wheel_path, version, sha256 = result
            assert version == "0.3.0"
            assert "0.3.0" in wheel_path.name

    def test_sha256_is_correct(self, tmp_path):
        """_get_latest_wheel returns the correct SHA256 of the wheel file."""
        import hashlib
        content = b"test wheel content for hashing"
        wheel = tmp_path / "comar_client-1.0.0-py3-none-any.whl"
        wheel.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()

        with patch.object(_mod, "_find_dist_dir", return_value=tmp_path):
            result = _mod._get_latest_wheel()
            assert result is not None
            _, _, sha256 = result
            assert sha256 == expected

    def test_compute_sha256_helper(self, tmp_path):
        """_compute_sha256 produces correct hex digest."""
        import hashlib
        content = b"hello world"
        f = tmp_path / "test.bin"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert _mod._compute_sha256(f) == expected
