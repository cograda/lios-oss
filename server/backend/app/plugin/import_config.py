"""One-time import: copy current HOME_* env values into `integration_config`.

    python -m app.plugin.import_config

For every integration's manifest `config_schema`, reads the raw `HOME_<KEY>`
environment variable (NOT the `HomeSettings` singleton — it was trimmed to
kernel/bootstrap fields only in this same chunk, so it no longer declares
these fields at all) and, if non-empty, writes it into the
`integration_config` table via `app.plugin.config_store.set_config_value`
— the same upsert path the config API uses, so secrets get Fernet-encrypted
identically.

Idempotent: re-running just re-writes the same values (upsert on
`(integration, key)`), so running it twice is a no-op in effect. Doesn't
touch keys with no non-empty env value — those stay on the env-fallback
path in `plugin_config()` until someone sets them (via this command again,
or the config API) or the key is dropped once the fallback period ends.

Requires `HOME_OAUTH_ENCRYPTION_KEY` to be set if any secret key has a
non-empty env value (fail-closed encryption — see app/auth/encryption.py).
"""

from __future__ import annotations

import logging
import os

from app.plugin.config_store import _coerce, set_config_value
from app.plugin.validate import discover_manifests

logger = logging.getLogger(__name__)


def import_all() -> dict[str, list[str]]:
    """Copy every integration's non-empty env-sourced config keys into the DB.

    Returns {integration: [keys written]} for reporting.
    """
    written: dict[str, list[str]] = {}
    for name, manifest in sorted(discover_manifests().items()):
        for key, spec in manifest.config_schema.items():
            env_value = os.environ.get(f"HOME_{key.upper()}")
            if env_value in (None, ""):
                continue
            set_config_value(name, key, _coerce(env_value, spec.type))
            written.setdefault(name, []).append(key)
            logger.info(f"[import_config] {name}.{key} <- HOME_{key.upper()}")
    return written


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = import_all()
    total = sum(len(v) for v in result.values())
    if not result:
        print("No env-sourced config values found to import.")
    else:
        for integration, keys in result.items():
            print(f"{integration}: {', '.join(keys)}")
        print(f"Imported {total} config value(s) across {len(result)} integration(s).")
