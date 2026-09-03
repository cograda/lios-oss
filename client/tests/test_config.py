"""Tests for client configuration loading."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from lios_sync.config import ClientConfig, load_config, CONFIG_FILE


class TestConfigDefaults:
    def test_default_auto_update_is_true(self):
        config = ClientConfig()
        assert config.auto_update is True

    def test_default_mcp_port(self):
        config = ClientConfig()
        assert config.mcp_port == 9400

    def test_default_server_url(self):
        config = ClientConfig()
        assert config.server.url == "localhost:9443"


class TestConfigLoading:
    def test_load_missing_file_returns_defaults(self):
        with patch("lios_sync.config.CONFIG_FILE", Path("/nonexistent/config.toml")):
            config = load_config()
            assert config.user == ""
            assert config.auto_update is True

    def test_load_config_with_auto_update_false(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
user = "alex"
mcp_port = 9400
auto_update = false

[server]
url = "localhost:9443"
token = "test-token"

[vault]
path = "/tmp/vault"
""")
        with patch("lios_sync.config.CONFIG_FILE", config_file):
            config = load_config()
            assert config.user == "alex"
            assert config.auto_update is False

    def test_load_config_without_auto_update_defaults_true(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
user = "sam"

[server]
url = "localhost:9443"
token = "abc"

[vault]
path = "/tmp/vault"
""")
        with patch("lios_sync.config.CONFIG_FILE", config_file):
            config = load_config()
            assert config.auto_update is True

    def test_load_config_parses_server_section_legacy_url(self, tmp_path):
        """Legacy `url = "..."` is converted to a single-element urls list."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
user = "alex"

[server]
url = "10.0.0.1:9999"
token = "secret"
""")
        with patch("lios_sync.config.CONFIG_FILE", config_file):
            config = load_config()
            assert config.server.urls == ["10.0.0.1:9999"]
            assert config.server.url == "10.0.0.1:9999"
            assert config.server.token == "secret"

    def test_load_config_default_reminders_section(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('user = "alex"\n')
        with patch("lios_sync.config.CONFIG_FILE", config_file):
            config = load_config()
            assert config.reminders.source_emails == {}

    def test_load_config_parses_reminders_source_emails(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
user = "alex"

[reminders.source_emails]
"AAAA-1111-2222-3333" = "alex@example.com"
"BBBB-4444-5555-6666" = "alex.work@example.com"
""")
        with patch("lios_sync.config.CONFIG_FILE", config_file):
            config = load_config()
            assert config.reminders.source_emails == {
                "AAAA-1111-2222-3333": "alex@example.com",
                "BBBB-4444-5555-6666": "alex.work@example.com",
            }

    def test_load_config_parses_server_urls_list(self, tmp_path):
        """New `urls = [...]` format with multiple addresses."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
user = "alex"

[server]
urls = ["192.168.1.50:9444", "100.82.221.91:9444"]
token = "secret"
""")
        with patch("lios_sync.config.CONFIG_FILE", config_file):
            config = load_config()
            assert config.server.urls == ["192.168.1.50:9444", "100.82.221.91:9444"]
            assert config.server.url == "192.168.1.50:9444"
            assert config.server.token == "secret"
