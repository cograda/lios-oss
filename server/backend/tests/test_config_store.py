"""Tests for app.plugin.config_store (V4 chunk 3.3).

Unit tier: pure helpers (_coerce/_serialize/_default_for) and
is_configured_from_schema's boolean logic, with plugin_config() itself
stubbed so nothing touches a real DB.
db tier: real round-trips through `integration_config` — secret encryption
at rest, env fallback, is_configured_from_schema against real rows, and
the one-time import command's idempotency.
"""

import json
import logging

import pytest
from cryptography.fernet import Fernet

from app.plugin import config_store
from app.plugin.manifest import ConfigFieldSpec


# ---------------------------------------------------------------------------
# Unit tier — pure coercion/serialization helpers
# ---------------------------------------------------------------------------

class TestCoerceAndSerialize:
    def test_bool_roundtrip(self):
        assert config_store._coerce("true", "bool") is True
        assert config_store._coerce("false", "bool") is False
        assert config_store._serialize(True, "bool") == "true"
        assert config_store._serialize(False, "bool") == "false"

    def test_int_roundtrip(self):
        assert config_store._coerce("42", "int") == 42
        assert config_store._serialize(42, "int") == "42"

    def test_float_roundtrip(self):
        """R4 (2026-09-04): added for the recency half-life config key — a
        days value that is not meaningfully an int."""
        assert config_store._coerce("30.5", "float") == 30.5
        assert config_store._serialize(30.5, "float") == "30.5"

    def test_list_str_roundtrip(self):
        assert config_store._coerce('["a", "b"]', "list_str") == ["a", "b"]
        assert json.loads(config_store._serialize(["a", "b"], "list_str")) == ["a", "b"]

    def test_dict_str_str_roundtrip(self):
        raw = config_store._serialize({"x": "full"}, "dict_str_str")
        assert config_store._coerce(raw, "dict_str_str") == {"x": "full"}

    def test_str_passthrough(self):
        assert config_store._coerce("hello", "str") == "hello"
        assert config_store._serialize("hello", "str") == "hello"


class TestDefaultFor:
    def test_explicit_default_wins(self):
        spec = ConfigFieldSpec(type="int", default=6)
        assert config_store._default_for(spec) == 6

    @pytest.mark.parametrize("type_name,zero", [
        ("str", ""), ("int", 0), ("float", 0.0), ("bool", False), ("list_str", []), ("dict_str_str", {}),
    ])
    def test_zero_value_when_unset(self, type_name, zero):
        spec = ConfigFieldSpec(type=type_name)
        assert config_store._default_for(spec) == zero


# ---------------------------------------------------------------------------
# Unit tier — is_configured_from_schema, plugin_config() stubbed
# ---------------------------------------------------------------------------

class TestIsConfiguredFromSchema:
    def test_no_required_keys_is_vacuously_true(self, monkeypatch):
        class _Manifest:
            config_schema = {"foo": ConfigFieldSpec(type="str", required=False)}

        monkeypatch.setattr(config_store, "_manifest_for", lambda name: _Manifest())
        assert config_store.is_configured_from_schema("whatever") is True

    def test_required_key_present_is_true(self, monkeypatch):
        class _Manifest:
            config_schema = {"api_key": ConfigFieldSpec(type="str", required=True)}

        class _Cfg:
            api_key = "set"

        monkeypatch.setattr(config_store, "_manifest_for", lambda name: _Manifest())
        monkeypatch.setattr(config_store, "plugin_config", lambda name: _Cfg())
        assert config_store.is_configured_from_schema("whatever") is True

    def test_required_key_missing_is_false(self, monkeypatch):
        class _Manifest:
            config_schema = {"api_key": ConfigFieldSpec(type="str", required=True)}

        class _Cfg:
            api_key = ""

        monkeypatch.setattr(config_store, "_manifest_for", lambda name: _Manifest())
        monkeypatch.setattr(config_store, "plugin_config", lambda name: _Cfg())
        assert config_store.is_configured_from_schema("whatever") is False


# ---------------------------------------------------------------------------
# db tier — real round-trips against integration_config
# ---------------------------------------------------------------------------

@pytest.mark.db
class TestPluginConfigRoundtrip:
    def test_secret_encrypted_at_rest(self, real_db, db_session):
        """lastfm_api_key is `secret=True` in the real manifest — the stored
        column value must be Fernet ciphertext, never the plaintext key."""
        from app.models.integration_config import IntegrationConfig

        config_store.set_config_value("lastfm", "lastfm_api_key", "super-secret-key")

        row = (
            db_session.query(IntegrationConfig)
            .filter_by(integration="lastfm", key="lastfm_api_key")
            .one()
        )
        assert row.is_secret is True
        assert row.value != "super-secret-key"
        assert row.value.startswith("gAAAAA")

        cfg = config_store.plugin_config("lastfm")
        assert cfg.lastfm_api_key == "super-secret-key"

    def test_non_secret_stored_plaintext(self, real_db, db_session):
        from app.models.integration_config import IntegrationConfig

        config_store.set_config_value("lastfm", "lastfm_username", "alex")
        row = (
            db_session.query(IntegrationConfig)
            .filter_by(integration="lastfm", key="lastfm_username")
            .one()
        )
        assert row.is_secret is False
        assert row.value == "alex"

    def test_typed_values_roundtrip_bool_and_int(self, real_db, db_session):
        config_store.set_config_value("commute", "commute_interchange_buffer_min", 9)
        config_store.set_config_value("commute", "commute_surface_decisions", True)

        cfg = config_store.plugin_config("commute")
        assert cfg.commute_interchange_buffer_min == 9
        assert cfg.commute_surface_decisions is True

    def test_dict_value_roundtrip(self, real_db, db_session):
        vis = {"a@gmail.com": "full", "b@gmail.com": "hidden"}
        config_store.set_config_value("google_calendar", "calendar_visibility", vis)
        cfg = config_store.plugin_config("google_calendar")
        assert cfg.calendar_visibility == vis

    def test_required_missing_reports_not_configured(self, real_db, db_session):
        # Uses homeassistant because this asserts the *generic* rule that every
        # `required` key must be set, which needs an integration with more than
        # one of them. (lastfm used to serve here, but went down to a single
        # required key when it became multi-user — neither username form can be
        # `required` once either one is sufficient.)
        assert config_store.is_configured_from_schema("homeassistant") is False
        config_store.set_config_value("homeassistant", "ha_url", "http://ha.example")
        # token still missing -> still not configured
        assert config_store.is_configured_from_schema("homeassistant") is False
        config_store.set_config_value("homeassistant", "ha_token", "t")
        assert config_store.is_configured_from_schema("homeassistant") is True

    def test_env_fallback_used_when_no_db_row(self, real_db, db_session, monkeypatch, caplog):
        # Env fallback reads raw os.environ (`HOME_<KEY>`), NOT the
        # HomeSettings singleton — it was trimmed to kernel/bootstrap fields
        # only in this same chunk and no longer declares these attributes.
        monkeypatch.setenv("HOME_LASTFM_API_KEY", "env-key")
        monkeypatch.setenv("HOME_LASTFM_USERNAME", "env-user")

        with caplog.at_level(logging.WARNING):
            cfg = config_store.plugin_config("lastfm")
        assert cfg.lastfm_api_key == "env-key"
        assert cfg.lastfm_username == "env-user"
        assert any("env fallback" in r.message or "HOME_*" in r.message for r in caplog.records)

    def test_env_fallback_warns_once_per_process(self, real_db, db_session, monkeypatch, caplog):
        # Use a key that hasn't been warned about yet in this test's run —
        # reset the module-level dedup set so this test is independent of
        # ordering relative to test_env_fallback_used_when_no_db_row above.
        config_store._warned.clear()
        monkeypatch.setenv("HOME_WHATSAPP_BRIDGE_URL", "http://bridge.local")

        with caplog.at_level(logging.WARNING):
            config_store.plugin_config("whatsapp")
            first_count = len(caplog.records)
            config_store.plugin_config("whatsapp")
            second_count = len(caplog.records)

        assert first_count == 1
        assert second_count == first_count  # no new warning on the second call


@pytest.mark.db
class TestIntegrationEnabledSwitch:
    """V4 chunk 5.1 — the enable/disable kernel switch, stored in the same
    `integration_config` table under the reserved `__enabled__` key."""

    def test_default_enabled_with_no_row(self, real_db, db_session):
        assert config_store.is_integration_enabled("lastfm") is True

    def test_disable_then_reenable_roundtrip(self, real_db, db_session):
        config_store.set_integration_enabled("lastfm", False)
        assert config_store.is_integration_enabled("lastfm") is False

        config_store.set_integration_enabled("lastfm", True)
        assert config_store.is_integration_enabled("lastfm") is True

    def test_switch_is_isolated_per_integration(self, real_db, db_session):
        config_store.set_integration_enabled("lastfm", False)
        assert config_store.is_integration_enabled("weather") is True


@pytest.mark.db
class TestImportConfig:
    def test_import_is_idempotent(self, real_db, db_session, monkeypatch):
        from app.models.integration_config import IntegrationConfig
        from app.plugin.import_config import import_all

        monkeypatch.setenv("HOME_LASTFM_API_KEY", "env-key")
        monkeypatch.setenv("HOME_LASTFM_USERNAME", "env-user")

        written_first = import_all()
        assert "lastfm" in written_first

        count_after_first = (
            db_session.query(IntegrationConfig)
            .filter_by(integration="lastfm", key="lastfm_api_key")
            .count()
        )
        assert count_after_first == 1

        written_second = import_all()
        assert "lastfm" in written_second

        count_after_second = (
            db_session.query(IntegrationConfig)
            .filter_by(integration="lastfm", key="lastfm_api_key")
            .count()
        )
        assert count_after_second == 1  # no duplicate row

        cfg = config_store.plugin_config("lastfm")
        assert cfg.lastfm_api_key == "env-key"
        assert cfg.lastfm_username == "env-user"

    def test_import_skips_empty_env_values(self, real_db, db_session):
        from app.models.integration_config import IntegrationConfig
        from app.plugin.import_config import import_all

        written = import_all()
        # No env vars set in this test -> nothing written for lastfm's
        # required-but-empty keys.
        assert "lastfm" not in written
        assert db_session.query(IntegrationConfig).count() == 0


@pytest.mark.db
class TestIsConfiguredRegressionAgainstRealManifests:
    """Every integration's is_configured() must evaluate the same way it did
    pre-3.3 against a config-table fixture mirroring today's real env
    values (server/.env.example) — the regression-safety net for this chunk.
    """

    def _set(self, integration, key, value):
        config_store.set_config_value(integration, key, value)

    def test_all_required_keys_set_reports_configured(self, real_db, db_session):
        # Mirrors an operator who has filled in every documented env var.
        self._set("lastfm", "lastfm_api_key", "k")
        self._set("lastfm", "lastfm_username", "u")
        self._set("homeassistant", "ha_url", "http://192.168.1.51:8123")
        self._set("homeassistant", "ha_token", "tok")
        self._set("commute", "nta_api_key", "k")
        self._set("apple_health", "health_push_token", "tok")

        for name in ("lastfm", "homeassistant", "commute", "apple_health"):
            assert config_store.is_configured_from_schema(name) is True

    def test_no_config_set_reports_not_configured_for_required_integrations(self, real_db, db_session):
        # Fresh DB, no env fallback either — every integration with a
        # required key must report not-configured (pre-3.3: `bool(settings.x)`
        # was False the same way when the env var was unset).
        for name in ("lastfm", "homeassistant", "commute", "apple_health"):
            assert config_store.is_configured_from_schema(name) is False

    def test_no_required_keys_integrations_always_configured(self, real_db, db_session):
        # Pre-3.3 these all had a bare `return True` override — the default
        # must still be vacuously True with nothing set.
        for name in (
            "finance", "irish_rail", "weather", "system", "historical_corpus",
            "attachments", "apple_reminders", "whatsapp", "snags", "media", "inbox",
        ):
            assert config_store.is_configured_from_schema(name) is True
