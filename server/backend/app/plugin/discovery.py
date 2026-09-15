"""Integration auto-discovery — V4 chunk 1.2.

Replaces the hand-maintained double list in the old
`app/integrations/__init__.py::register_all()` (one import line + one
`register(...)` call per integration, 19 entries each, that had to be kept
in sync by hand). Discovery walks `app/integrations/*` on disk, exactly like
`app.plugin.validate.discover_manifests()` does for manifests, imports each
integration's `manifest.py` (fail loud if it's missing — 1.1 already
enforces manifests exist; this is the second consumer) then its package
`__init__.py`, finds the concrete `BaseIntegration` subclass defined there,
instantiates it, and registers it.

Order is deterministic: integrations are visited in sorted-by-name order (a
plain `sorted(Path.iterdir())` walk), so registration order — and therefore
iteration order over `app.integrations.get_all()` — never depends on
filesystem/readdir ordering.

A package under `app/integrations/` that isn't itself an integration (e.g.
`sheets`, a reusable Sheets-writer library with no `BaseIntegration`
subclass) is skipped, same rule as `validate.discover_manifests()`.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from app.integrations.base import BaseIntegration


def _integrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "integrations"


def _find_integration_class(module) -> type[BaseIntegration] | None:
    """Return the concrete BaseIntegration subclass defined *in* this module.

    `inspect.getmodule(obj) is module` excludes classes merely imported into
    the package's `__init__.py` namespace (e.g. `from app.integrations.base
    import BaseIntegration` itself), so we only ever pick up the class the
    package actually defines.
    """
    for obj in vars(module).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseIntegration)
            and obj is not BaseIntegration
            and inspect.getmodule(obj) is module
        ):
            return obj
    return None


def discover_integrations() -> list[BaseIntegration]:
    """Import every integration package under `app/integrations/` and
    instantiate its `BaseIntegration`.

    Returns instances sorted by package name (deterministic order). Skips
    packages with no `BaseIntegration` subclass (libraries like `sheets`).
    Raises if a package that *does* define one has no `manifest.py` — the
    same invariant `app.plugin.validate.discover_manifests()` enforces,
    checked again here because discovery is the mechanism that actually
    needs the import to succeed at registration time.
    """
    instances: list[BaseIntegration] = []
    for subdir in sorted(_integrations_dir().iterdir(), key=lambda p: p.name):
        if not subdir.is_dir() or subdir.name.startswith("_") or subdir.name == "__pycache__":
            continue
        if not (subdir / "__init__.py").exists():
            continue

        module = importlib.import_module(f"app.integrations.{subdir.name}")
        integration_cls = _find_integration_class(module)
        if integration_cls is None:
            continue  # e.g. `sheets` — a library, not an integration

        # Manifest must exist and import cleanly (rule enforced again here,
        # not just in validate.discover_manifests(), so a missing manifest
        # fails at the point registration actually depends on it).
        importlib.import_module(f"app.integrations.{subdir.name}.manifest")

        instances.append(integration_cls())

    return instances


def discover_integration_models() -> dict[str, type]:
    """Return {model class name: class} for every model any integration's
    manifest declares owning (`manifest.models`).

    Used by `app.models.__init__` so ORM classes reach `create_tables()` and
    Alembic's autogenerate without a hand-maintained import block — adding a
    model to an integration means adding its name to that integration's own
    `manifest.py`, nothing else.

    Iterates manifests (not `discover_integrations()`'s instantiated
    integrations) in the same sorted-by-name order, via
    `app.plugin.validate.discover_manifests()` — the single place that
    knows how to walk `app/integrations/*` and tell a real integration
    package apart from a library like `sheets` (no manifest, skipped).
    """
    from app.plugin.validate import discover_manifests

    models: dict[str, type] = {}
    for name, manifest in sorted(discover_manifests().items()):
        if not manifest.models:
            continue
        models_module = importlib.import_module(f"app.integrations.{name}.models")
        for model_name in manifest.models:
            models[model_name] = getattr(models_module, model_name)

    return models
