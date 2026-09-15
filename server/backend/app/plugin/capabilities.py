"""Capability registry — V4 chunk 4.2.

`app/plugin/manifest.py`'s `provides` field lets an integration declare named
capabilities other plugins may depend on (`depends_on`). This module is the
one runtime consumer of that declaration: `get_capability(name)` resolves a
capability name to the *facade object* the providing integration registered
for it.

Convention, kept deliberately simple (no separate registration call): the
owning integration exposes its facade as a module-level singleton named
`FACADE` in its own `app/integrations/<name>/facade.py`. `get_capability()`
looks up which integration's manifest declares `name` in `provides`, imports
that integration's `facade` module, and returns its `FACADE`. This is the
kernel-side half of "no raw cross-package imports" (V4 chunk 4.2): callers
that need another integration's behaviour ask the kernel for the capability
by name (or, for edges that are simple 1:1 dependencies, import the facade
module directly — either way, `<pkg>.facade` is the only thing another
package is ever allowed to import from `app.integrations.<pkg>`).

Capability *enforcement* — deciding whether a given caller is allowed to
invoke a given capability — is chunk 2.2 (deliberately on hold pending
sam-rollout Phase B-D). This module is pure wiring: it answers "who
provides X", nothing more.
"""

from __future__ import annotations

import importlib
from typing import Any

from app.plugin.validate import discover_manifests


class CapabilityNotFoundError(RuntimeError):
    """Raised when no integration's manifest declares the requested capability,
    or the declared provider's `facade.py` doesn't export `FACADE`."""


def _provider_index() -> dict[str, str]:
    """capability name -> owning integration package name.

    Recomputed on every call (manifest discovery itself is cheap and already
    memoizes nothing — see `discover_manifests()` — so there's no separate
    cache to invalidate in tests that swap manifests around)."""
    manifests = discover_manifests()
    index: dict[str, str] = {}
    for name, manifest in manifests.items():
        for capability in manifest.provides:
            index[capability] = name
    return index


def get_capability(name: str) -> Any:
    """Return the facade object registered for capability `name`.

    Raises `CapabilityNotFoundError` if no integration's manifest declares
    `name` in its `provides` list, or if the declared provider's
    `facade.py` module doesn't export a `FACADE` object.
    """
    owner = _provider_index().get(name)
    if owner is None:
        raise CapabilityNotFoundError(f"no integration provides capability {name!r}")

    module = importlib.import_module(f"app.integrations.{owner}.facade")
    facade = getattr(module, "FACADE", None)
    if facade is None:
        raise CapabilityNotFoundError(
            f"app.integrations.{owner}.facade must export FACADE "
            f"(declares capability {name!r} in its manifest)"
        )
    return facade
