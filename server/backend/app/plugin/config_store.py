"""Per-integration config accessor — V4 chunk 3.3.

Every integration used to read its config straight off the global
`HomeSettings` singleton (`settings.lastfm_api_key`, `settings.ha_url`, ...).
That mixed kernel concerns (db, ports, the encryption key) with ~20
integration-specific keys in one flat pydantic model, and meant "add a
config key" was always a `config.py` edit.

This module replaces that per-integration read with `plugin_config(name)`:
a typed pydantic model built from the integration's manifest
`config_schema`, with values sourced from the `integration_config` DB table
and falling back to the raw `HOME_<KEY>` environment variable when the
table has no row yet (transition period — see `import_config.py` for the
one-time copy, and `.env.example` for which env vars still matter
operationally). This reads `os.environ` directly, NOT the `HomeSettings`
singleton — `HomeSettings` was deliberately trimmed to kernel/bootstrap
fields only in this same chunk, so it no longer declares
`lastfm_api_key`/`ha_url`/etc. at all; falling back to `getattr(settings, key)`
would silently return None for every integration-specific key forever.
Env fallback is logged once per (integration, key) per process — not an
error, just a nudge that the value hasn't been migrated into the table.

Secrets are Fernet-encrypted at rest via `app.auth.encryption` — reading one
back from the DB decrypts it; reading one from the env fallback returns it
as-is (env values were never encrypted to begin with).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from pydantic import BaseModel, create_model

from app.auth.encryption import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

# Reserved kernel-level config key — not part of any integration's own
# `config_schema` (V4 chunk 5.1). Lives in the same `integration_config`
# table as everything else so the enable/disable switch persists and
# survives restarts without a dedicated table, but it's read/written
# through the two functions below rather than `plugin_config()`/
# `set_config_value()` (which validate the key against the manifest
# schema and would reject it).
_ENABLED_KEY = "__enabled__"

_PY_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list_str": list,
    "dict_str_str": dict,
}

_warned: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


def _warn_env_fallback_once(integration: str, key: str) -> None:
    with _warned_lock:
        marker = (integration, key)
        if marker in _warned:
            return
        _warned.add(marker)
    logger.warning(
        f"[config] {integration}.{key} sourced from HOME_* env fallback, not "
        f"the integration_config table — run `python -m app.plugin.import_config` "
        f"to migrate it (env fallback is a transition-period convenience, not permanent)."
    )


def _coerce(raw: str, type_name: str) -> Any:
    if type_name == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(raw)
    if type_name == "float":
        return float(raw)
    if type_name in ("list_str", "dict_str_str"):
        return json.loads(raw)
    return raw


def _serialize(value: Any, type_name: str) -> str:
    if type_name == "bool":
        return "true" if value else "false"
    if type_name in ("int", "float"):
        return str(value)
    if type_name in ("list_str", "dict_str_str"):
        return json.dumps(value)
    return str(value)


_ZERO_VALUES: dict[str, Any] = {
    "str": "", "int": 0, "float": 0.0, "bool": False, "list_str": [], "dict_str_str": {},
}


def _default_for(spec: Any) -> Any:
    """The field's declared default, or the type's zero-value if unset.

    Falling back to a same-typed zero-value (not bare `None`) keeps the
    generated pydantic model's field type honest (`str`, not `str | None`)
    and keeps `bool(value)`-style required-key checks well-defined.
    """
    return spec.default if spec.default is not None else _ZERO_VALUES[spec.type]


def _build_model(integration: str, schema: dict[str, Any]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for key, spec in schema.items():
        py_type = _PY_TYPES[spec.type]
        fields[key] = (py_type, _default_for(spec))
    model_name = "".join(p.title() for p in integration.split("_")) + "Config"
    return create_model(model_name, **fields)  # type: ignore[call-overload]


def _manifest_for(integration: str):
    from app.plugin.validate import discover_manifests

    manifest = discover_manifests().get(integration)
    if manifest is None:
        raise ValueError(f"plugin_config: unknown integration {integration!r}")
    return manifest


def plugin_config(integration: str) -> BaseModel:
    """Return a typed config object for `integration`, built from its manifest
    `config_schema`. Values: DB row (decrypted if secret) -> env fallback
    (warns once) -> schema default.
    """
    from app.db import get_db
    from app.models.integration_config import IntegrationConfig

    manifest = _manifest_for(integration)
    schema = manifest.config_schema
    model_cls = _build_model(integration, schema)

    if not schema:
        return model_cls()

    db = get_db()
    with db.session() as session:
        rows = {
            row.key: row
            for row in session.query(IntegrationConfig)
            .filter_by(integration=integration)
            .all()
        }

        values: dict[str, Any] = {}
        for key, spec in schema.items():
            row = rows.get(key)
            if row is not None:
                raw = decrypt_token(row.value) if row.is_secret else row.value
                values[key] = _coerce(raw, spec.type) if raw != "" else _default_for(spec)
                continue

            env_value = os.environ.get(f"HOME_{key.upper()}")
            if env_value not in (None, ""):
                _warn_env_fallback_once(integration, key)
                values[key] = _coerce(env_value, spec.type)
            else:
                values[key] = _default_for(spec)

    return model_cls(**values)


def is_configured_from_schema(integration: str) -> bool:
    """Default `is_configured()`: True iff every `required` config_schema key
    resolves to a truthy value (DB or env fallback).
    """
    manifest = _manifest_for(integration)
    schema = manifest.config_schema
    required = [k for k, spec in schema.items() if spec.required]
    if not required:
        return True

    cfg = plugin_config(integration)
    return all(bool(getattr(cfg, key)) for key in required)


def is_integration_enabled(integration: str) -> bool:
    """Whether `integration` is enabled (V4 chunk 5.1's kernel-level switch).

    Default is True — an integration with no row in `integration_config` for
    the reserved `__enabled__` key is enabled, so a fresh boot (no admin has
    ever touched this) behaves identically to before this chunk. Distinct
    from `is_configured()`: a disabled integration can be fully configured,
    and a configured integration can be disabled.
    """
    from app.db import get_db
    from app.models.integration_config import IntegrationConfig

    db = get_db()
    with db.session() as session:
        row = (
            session.query(IntegrationConfig)
            .filter_by(integration=integration, key=_ENABLED_KEY)
            .first()
        )
        if row is None:
            return True
        return row.value.strip().lower() in ("1", "true", "yes", "on")


def set_integration_enabled(
    integration: str, enabled: bool, *, updated_by: int | None = None,
) -> None:
    """Flip the enable/disable switch for `integration`.

    Does not validate `integration` against `discover_manifests()` — the
    caller (the `/integrations/{name}/enabled` route) already 404s on an
    unknown name before this is reached, and keeping this function itself
    manifest-agnostic means it never needs the extra DB round-trip
    `_manifest_for()` would cost.
    """
    from app.db import get_db
    from app.models.integration_config import IntegrationConfig

    db = get_db()
    with db.session() as session:
        row = (
            session.query(IntegrationConfig)
            .filter_by(integration=integration, key=_ENABLED_KEY)
            .first()
        )
        if row is None:
            row = IntegrationConfig(integration=integration, key=_ENABLED_KEY, is_secret=False)
            session.add(row)
        row.value = "true" if enabled else "false"
        row.updated_by = updated_by
        session.commit()


def set_config_value(
    integration: str,
    key: str,
    value: Any,
    *,
    updated_by: int | None = None,
) -> None:
    """Upsert one config value (used by the import command and the config API).

    `value` must already be the right Python type for the field (str/int/bool/
    list/dict) — this function handles serialization + secret encryption.
    """
    from app.db import get_db
    from app.models.integration_config import IntegrationConfig

    manifest = _manifest_for(integration)
    spec = manifest.config_schema.get(key)
    if spec is None:
        raise ValueError(f"{integration!r} has no config_schema key {key!r}")

    raw = _serialize(value, spec.type)
    if spec.secret and raw:
        raw = encrypt_token(raw)

    db = get_db()
    with db.session() as session:
        row = (
            session.query(IntegrationConfig)
            .filter_by(integration=integration, key=key)
            .first()
        )
        if row is None:
            row = IntegrationConfig(integration=integration, key=key, is_secret=spec.secret)
            session.add(row)
        row.value = raw
        row.is_secret = spec.secret
        row.updated_by = updated_by
        session.commit()
