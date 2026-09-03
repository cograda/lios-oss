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
        (tmp_path / "lios_sync-0.1.0-py3-none-any.whl").touch()
        (tmp_path / "lios_sync-0.2.0-py3-none-any.whl").touch()

        wheels = sorted(tmp_path.glob("lios_sync-*.whl"), reverse=True)
        assert len(wheels) == 2
        assert "0.2.0" in wheels[0].name

    def test_version_extraction_from_wheel_name(self):
        """Regex should extract version from standard wheel filename."""
        name = "lios_sync-1.2.3-py3-none-any.whl"
        m = re.search(r"lios_sync-([^-]+)-", name)
        assert m is not None
        assert m.group(1) == "1.2.3"

    def test_version_extraction_with_prerelease(self):
        name = "lios_sync-1.2.3rc1-py3-none-any.whl"
        m = re.search(r"lios_sync-([^-]+)-", name)
        assert m is not None
        assert m.group(1) == "1.2.3rc1"

    def test_no_wheels_returns_none(self, tmp_path):
        wheels = sorted(tmp_path.glob("lios_sync-*.whl"), reverse=True)
        assert len(wheels) == 0

    def test_filename_validation_regex(self):
        """The download endpoint validates filenames — test the pattern."""
        pattern = r"^lios_sync-[\w.]+-[\w.]+-[\w.]+-[\w.]+\.whl$"
        assert re.match(pattern, "lios_sync-0.1.0-py3-none-any.whl")
        assert not re.match(pattern, "../../../etc/passwd")
        assert not re.match(pattern, "malicious.whl")
        assert not re.match(pattern, "lios_sync-0.1.0-py3-none-any.tar.gz")


# ---------------------------------------------------------------------------
# Tests that need the actual client_dist module
# ---------------------------------------------------------------------------

_dist_path = Path(__file__).resolve().parent.parent / "app" / "routes" / "client_dist.py"


def _load_client_dist():
    """Load client_dist.py in isolation, stubbing FastAPI/app.config only for
    the duration of the load. Leaving stubs in sys.modules poisons real
    `app.*` imports for later tests in the same process (collection-order
    dependent) — the pattern the Phase 1 test cleanup retired."""
    added: list[str] = []

    def _stub(name: str, mod) -> None:
        if name not in sys.modules:
            sys.modules[name] = mod
            added.append(name)

    _stub("fastapi", MagicMock())
    _stub("fastapi.responses", MagicMock())
    _stub("app", ModuleType("app"))
    _config_mod = ModuleType("app.config")
    _config_mod.settings = MagicMock()
    _config_mod.settings.server_public_url = "https://comar.lab"
    _stub("app.config", _config_mod)
    try:
        spec = importlib.util.spec_from_file_location("client_dist", str(_dist_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None
    finally:
        for name in added:
            sys.modules.pop(name, None)


_mod = _load_client_dist()
_HAS_CLIENT_DIST = _mod is not None


@pytest.mark.skipif(not _HAS_CLIENT_DIST, reason="Cannot load client_dist (missing dep)")
class TestClientDistModule:
    def test_no_dist_dir_returns_none(self):
        """If no distribution directory exists, _find_dist_dir returns None."""
        with patch.object(_mod, "_DIST_DIRS", [Path("/nonexistent/a"), Path("/nonexistent/b")]):
            assert _mod._find_dist_dir() is None

    def test_get_latest_wheel_with_dist_dir(self, tmp_path):
        """_get_latest_wheel finds the newest wheel in a real directory."""
        (tmp_path / "lios_sync-0.1.0-py3-none-any.whl").write_bytes(b"old")
        (tmp_path / "lios_sync-0.3.0-py3-none-any.whl").write_bytes(b"new")
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
        wheel = tmp_path / "lios_sync-1.0.0-py3-none-any.whl"
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

    def test_picks_10_0_0_over_2_3_2(self, tmp_path):
        """Version-aware picking, not lexical filename sort.

        A lexical sort ("lios_sync-10.0.0-..." < "lios_sync-2.3.2-...")
        would wrongly pick 2.3.2 as "latest". This is the exact bug that let
        a stale committed 0.1.0 wheel shadow real releases for months.
        """
        (tmp_path / "lios_sync-2.3.2-py3-none-any.whl").write_bytes(b"a")
        (tmp_path / "lios_sync-10.0.0-py3-none-any.whl").write_bytes(b"b")
        (tmp_path / "lios_sync-9.9.9-py3-none-any.whl").write_bytes(b"c")

        with patch.object(_mod, "_find_dist_dir", return_value=tmp_path):
            result = _mod._get_latest_wheel()
            assert result is not None
            wheel_path, version, _ = result
            assert version == "10.0.0"
            assert wheel_path.name == "lios_sync-10.0.0-py3-none-any.whl"

    def test_empty_dist_dir_returns_none(self, tmp_path):
        """An existing but empty client-dist/ directory yields no wheel, not an error."""
        with patch.object(_mod, "_find_dist_dir", return_value=tmp_path):
            assert _mod._get_latest_wheel() is None

    def test_pick_latest_wheel_ignores_unparseable_filenames(self, tmp_path):
        """A stray file that doesn't match the version pattern is never picked."""
        good = tmp_path / "lios_sync-1.0.0-py3-none-any.whl"
        good.write_bytes(b"a")
        junk = tmp_path / "lios_sync-py3-none-any.whl"
        junk.write_bytes(b"b")

        picked = _mod.pick_latest_wheel([good, junk])
        assert picked == good

    def test_pick_latest_wheel_empty_iterable_returns_none(self):
        assert _mod.pick_latest_wheel([]) is None


# ---------------------------------------------------------------------------
# `_latest_client_info` (app/api/v1.py) shares the same picker — verify it
# actually delegates rather than carrying its own lexical-sort copy, and
# behaves correctly end-to-end (this is the heartbeat path whose lexical
# sort + CI path bug served a stale 0.1.0 wheel in production).
# ---------------------------------------------------------------------------

class TestLatestClientInfo:
    def test_picks_highest_version_not_lexically_last(self, tmp_path, monkeypatch):
        from app.api.v1 import _latest_client_info
        from app.routes import client_dist

        (tmp_path / "lios_sync-2.3.2-py3-none-any.whl").write_bytes(b"a")
        (tmp_path / "lios_sync-10.0.0-py3-none-any.whl").write_bytes(b"b")

        monkeypatch.setattr(client_dist, "_DIST_DIRS", [tmp_path])

        version, checksum = _latest_client_info()
        assert version == "10.0.0"
        assert len(checksum) == 64  # sha256 hex digest

    def test_empty_dir_returns_blank_tuple(self, tmp_path, monkeypatch):
        from app.api.v1 import _latest_client_info
        from app.routes import client_dist

        monkeypatch.setattr(client_dist, "_DIST_DIRS", [tmp_path])

        assert _latest_client_info() == ("", "")


class TestLatestClientInfoDelegation:
    def test_v1_delegates_to_shared_picker(self):
        v1_src = (
            Path(__file__).resolve().parent.parent / "app" / "api" / "v1.py"
        ).read_text()
        assert "def _latest_client_info" in v1_src
        fn_body = v1_src.split("def _latest_client_info", 1)[1].split("\ndef ", 1)[0]
        assert "pick_latest_wheel" in fn_body, (
            "_latest_client_info must delegate to routes.client_dist.pick_latest_wheel"
        )
        assert "sorted(" not in fn_body, (
            "_latest_client_info must not reintroduce lexical filename sorting "
            "(that bug served a 0.1.0 wheel as 'latest' — see improvement-round-2026-07 Chunk A)"
        )


# ---------------------------------------------------------------------------
# Download gating (V4 chunk 2.5) — /version and /bootstrap.sh stay open;
# /download/* now requires a valid bearer OR a live install code. Exercises
# `require_download_auth` directly (no FastAPI TestClient/app boot needed —
# these are unit-tier, no DB) with a fake Request-shaped object.
# ---------------------------------------------------------------------------

class _FakeClientAddr:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    """Just enough of starlette.Request's surface for require_download_auth."""

    def __init__(self, host="203.0.113.5", query=None):
        self.client = _FakeClientAddr(host) if host else None
        self._query = query or {}

    @property
    def query_params(self):
        return self._query


@pytest.fixture
def capture_auth_events(monkeypatch):
    """Fake get_db()/session() that records every AuthEvent added, so tests
    can assert record_auth_event actually fired without a real database."""
    added: list = []

    class _FakeSession:
        def add(self, obj):
            added.append(obj)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeDB:
        def session(self):
            return _FakeSession()

    import app.services.auth_events as auth_events_mod

    monkeypatch.setattr(auth_events_mod, "get_db", lambda: _FakeDB())
    return added


class TestDownloadGating:
    def test_download_endpoints_require_auth_dependency(self):
        """/download/latest and /download/{filename} are wired to the gate;
        /version and /bootstrap.sh are deliberately left open (see the
        module docstring's onboarding-safety reasoning)."""
        import inspect

        from app.routes import client_dist

        latest_params = inspect.signature(client_dist.download_latest).parameters
        assert "_auth" in latest_params

        by_name_params = inspect.signature(client_dist.download_wheel).parameters
        assert "_auth" in by_name_params

        version_params = inspect.signature(client_dist.client_version).parameters
        assert "_auth" not in version_params

        bootstrap_params = inspect.signature(client_dist.bootstrap_script).parameters
        assert "_auth" not in bootstrap_params

    def test_download_rejected_without_bearer_or_code(self, monkeypatch, capture_auth_events):
        from fastapi import HTTPException

        from app.routes import client_dist
        import app.auth.client_token as client_token_mod

        monkeypatch.setattr(client_token_mod, "resolve_token_to_user", lambda token: None)
        monkeypatch.setattr(client_dist, "_install_code_is_live", lambda code: False)

        req = _FakeRequest()
        with pytest.raises(HTTPException) as exc_info:
            client_dist.require_download_auth(req, authorization="")

        assert exc_info.value.status_code == 401

    def test_401_writes_an_auth_events_row(self, monkeypatch, capture_auth_events):
        from fastapi import HTTPException

        from app.routes import client_dist
        import app.auth.client_token as client_token_mod

        monkeypatch.setattr(client_token_mod, "resolve_token_to_user", lambda token: None)
        monkeypatch.setattr(client_dist, "_install_code_is_live", lambda code: False)

        req = _FakeRequest(host="198.51.100.9")
        with pytest.raises(HTTPException):
            client_dist.require_download_auth(req, authorization="Bearer deadbeef")

        assert len(capture_auth_events) == 1
        row = capture_auth_events[0]
        assert row.outcome == "401"
        assert row.transport == "http"
        assert row.source_ip == "198.51.100.9"
        assert row.token_last4 == "beef"

    def test_download_allowed_with_valid_bearer(self, monkeypatch, capture_auth_events):
        from app.routes import client_dist
        import app.auth.client_token as client_token_mod

        fake_user = object()
        monkeypatch.setattr(client_token_mod, "resolve_token_to_user", lambda token: fake_user)
        monkeypatch.setattr(client_dist, "_install_code_is_live", lambda code: False)

        req = _FakeRequest()
        # Must not raise.
        client_dist.require_download_auth(req, authorization="Bearer good-token")
        assert capture_auth_events == []  # no 401 logged on the happy path

    def test_fresh_install_code_only_download_succeeds_no_bearer(
        self, monkeypatch, capture_auth_events
    ):
        """The new-machine-bootstrap safety case: a device that only has an
        install code (no bearer yet) must still be able to authorize a
        wheel download by passing `?code=`."""
        from app.routes import client_dist
        import app.auth.client_token as client_token_mod

        # No bearer resolves — this device has never had one.
        monkeypatch.setattr(client_token_mod, "resolve_token_to_user", lambda token: None)
        # The install code is live (unexpired).
        monkeypatch.setattr(
            client_dist, "_install_code_is_live",
            lambda code: code == "fresh-machine-code",
        )

        req = _FakeRequest(query={"code": "fresh-machine-code"})
        # Must not raise — no Authorization header at all.
        client_dist.require_download_auth(req, authorization="")
        assert capture_auth_events == []


class TestBootstrapScriptSendsBearerOnDownload:
    """Regression guard for the legacy bootstrap.sh flow: since /download/*
    now requires auth, the wheel-download curl inside the generated script
    must carry the TOKEN the caller was handed as argv — otherwise gating
    the endpoint would silently break this onboarding path."""

    def test_bootstrap_template_sends_bearer_on_wheel_download(self):
        from app.routes.client_dist import BOOTSTRAP_TEMPLATE

        download_line = next(
            line for line in BOOTSTRAP_TEMPLATE.splitlines()
            if "download/latest" in line
        )
        assert "Authorization: Bearer" in download_line
        assert "$TOKEN" in download_line



class TestLegacyChannelFreeze:
    """2.x `comar` daemons must never be told about a `lios_sync` wheel."""

    def test_two_x_clients_are_legacy(self):
        from app.api.v1 import client_is_legacy
        assert client_is_legacy("2.6.3") is True
        assert client_is_legacy("2.99.0") is True

    def test_three_x_and_unknown_are_not(self):
        from app.api.v1 import client_is_legacy
        assert client_is_legacy("3.0.0") is False
        assert client_is_legacy("") is False
        assert client_is_legacy("dev") is False
