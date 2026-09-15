"""Dotted-ref resolution — V4 chunk 3.1.

Manifests point at code with plain strings (`"pkg.mod:attr"`) rather than
importing it directly, so the kernel never has to `from app.integrations.X
import Y`. This is the one place that turns such a string back into the
real object, at the point something actually needs to call/mount it
(scheduler cron jobs, startup tasks, route mounting).

Same format `app.services.data_freshness._probe()` already used inline for
`StalenessProbe.probe_function` — centralised here so every consumer (kernel
jobs, background tasks, routes) shares one implementation.
"""

from __future__ import annotations

import importlib
from typing import Any


def resolve_ref(ref: str) -> Any:
    """Resolve `"pkg.mod:attr"` to the live object `pkg.mod.attr`."""
    module_path, _, attr_name = ref.partition(":")
    if not attr_name:
        raise ValueError(f"invalid dotted ref (expected 'module:attr'): {ref!r}")
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)
