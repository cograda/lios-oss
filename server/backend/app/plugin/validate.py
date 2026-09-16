"""Startup manifest validation — V4 chunk 1.1.

Fails loud (raises) if any integration's manifest is missing or internally
inconsistent. Called from `app/main.py`'s lifespan, after `register_all()`
(so the real `BaseIntegration` instances are available to check schedules
and tool names against) and before the app serves any traffic.

Two entry points:
  - `discover_manifests()` walks `app/integrations/*` on disk and imports
    each package's `manifest.py`, enforcing rule 1 (every integration
    package has one).
  - `validate_manifests(manifests, integrations)` takes a manifest dict (real
    or, in tests, hand-built) plus the corresponding registered integration
    instances and enforces rules 2-5. It's a pure function over its
    arguments so tests can feed it deliberately-broken manifests without
    touching the real integration tree.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING

from app.integrations.base import BaseIntegration
from app.plugin.manifest import IntegrationManifest

if TYPE_CHECKING:
    pass


class ManifestValidationError(RuntimeError):
    """Raised when an integration manifest is missing or inconsistent.

    Boot must fail loudly on this — a silently-wrong manifest defeats the
    entire point of having one (later chunks drive scheduling, freshness,
    and registration off these).
    """


def _integrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "integrations"


def _is_integration_package(module) -> bool:
    """True if this module defines a concrete BaseIntegration subclass.

    Distinguishes real integrations (need a manifest) from shared libraries
    like `sheets`, which lives under `app/integrations/` but isn't itself an
    integration (no manifest — see chunk 1.1 spec).
    """
    return any(
        isinstance(obj, type) and issubclass(obj, BaseIntegration) and obj is not BaseIntegration
        for obj in vars(module).values()
    )


def discover_manifests() -> dict[str, IntegrationManifest]:
    """Scan `app/integrations/*` and import each integration's manifest.

    Raises `ManifestValidationError` (rule 1) if a package that defines a
    `BaseIntegration` subclass doesn't have a `manifest.py` exporting
    `MANIFEST`.
    """
    manifests: dict[str, IntegrationManifest] = {}
    for subdir in sorted(_integrations_dir().iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("_") or subdir.name == "__pycache__":
            continue
        if not (subdir / "__init__.py").exists():
            continue

        module = importlib.import_module(f"app.integrations.{subdir.name}")
        if not _is_integration_package(module):
            continue  # e.g. `sheets` — a library, not an integration

        manifest_module_name = f"app.integrations.{subdir.name}.manifest"
        try:
            manifest_module = importlib.import_module(manifest_module_name)
        except ModuleNotFoundError as exc:
            raise ManifestValidationError(
                f"{subdir.name}: no manifest.py found (every integration "
                f"package needs one — see app/plugin/manifest.py)"
            ) from exc

        manifest = getattr(manifest_module, "MANIFEST", None)
        if manifest is None or not isinstance(manifest, IntegrationManifest):
            raise ManifestValidationError(
                f"{subdir.name}.manifest must export MANIFEST: IntegrationManifest"
            )
        manifests[subdir.name] = manifest

    return manifests


def _check_capability_providers(manifests: dict[str, IntegrationManifest]) -> dict[str, str]:
    """Rule 7 (V4 chunk 4.2): every `provides` capability string is claimed by
    exactly one integration. Returns the capability -> owner map for reuse by
    `_check_dependency_graph`.

    Same "no silent double-claim" logic as `_check_unique_embedding_sources`
    — two integrations declaring the same capability would make
    `get_capability()` resolution ambiguous (in practice: whichever module
    happens to be discovered second silently wins).
    """
    providers: dict[str, str] = {}
    for name, manifest in manifests.items():
        for capability in manifest.provides:
            if capability in providers:
                raise ManifestValidationError(
                    f"Duplicate capability {capability!r}: provided by both "
                    f"{providers[capability]!r} and {name!r}"
                )
            providers[capability] = name
    return providers


def _check_dependency_graph(manifests: dict[str, IntegrationManifest]) -> None:
    """Rule 5 (V4 chunk 4.2): every `depends_on` entry names a *capability*
    that some manifest's `provides` list actually declares, and the resulting
    integration-level dependency graph (dependent -> capability's owner) is
    acyclic.

    `depends_on` used to name integration package names directly; as of this
    chunk it names capability strings instead (e.g. "mail.query"), so a
    dependency is a real, checkable contract — not just a package name that
    happens to still exist.
    """
    providers = _check_capability_providers(manifests)

    for name, manifest in manifests.items():
        for dep in manifest.depends_on:
            if dep not in providers:
                raise ManifestValidationError(
                    f"{name}: depends_on references unknown capability {dep!r} "
                    f"(no integration's manifest declares it in `provides`)"
                )

    # Cycle detection over the integration-level graph induced by capability
    # ownership: name -> {owner of each capability name depends_on}.
    edges: dict[str, set[str]] = {name: set() for name in manifests}
    for name, manifest in manifests.items():
        for dep in manifest.depends_on:
            owner = providers[dep]
            if owner != name:
                edges[name].add(owner)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in manifests}

    def visit(name: str, path: list[str]) -> None:
        color[name] = GRAY
        for dep in edges[name]:
            if color[dep] == GRAY:
                cycle = " -> ".join(path + [dep])
                raise ManifestValidationError(
                    f"depends_on cycle detected: {cycle}"
                )
            if color[dep] == WHITE:
                visit(dep, path + [dep])
        color[name] = BLACK

    for name in manifests:
        if color[name] == WHITE:
            visit(name, [name])


def _check_models(name: str, manifest: IntegrationManifest) -> None:
    """Rule 2: `models` names resolve to classes in the package's models.py."""
    if not manifest.models:
        return
    try:
        models_module = importlib.import_module(f"app.integrations.{name}.models")
    except ModuleNotFoundError as exc:
        raise ManifestValidationError(
            f"{name}: manifest declares models {manifest.models!r} but the "
            f"package has no models.py"
        ) from exc
    for model_name in manifest.models:
        if not hasattr(models_module, model_name):
            raise ManifestValidationError(
                f"{name}: manifest declares model {model_name!r}, but no "
                f"such class exists in app.integrations.{name}.models"
            )


def _check_matches_abc(name: str, manifest: IntegrationManifest, integration: BaseIntegration) -> None:
    """Rule 3: name / display_name match the live ABC properties."""
    if manifest.name != integration.name:
        raise ManifestValidationError(
            f"{name}: manifest.name={manifest.name!r} != integration.name={integration.name!r}"
        )
    if manifest.display_name != integration.display_name:
        raise ManifestValidationError(
            f"{name}: manifest.display_name={manifest.display_name!r} != "
            f"integration.display_name={integration.display_name!r}"
        )
    # Schedule used to be double-checked against `integration.sync_schedule()`
    # here — V4 chunk 3.1 deleted that ABC method (and `sync_timezone()`)
    # once the kernel scheduler started reading `manifest.schedule` /
    # `schedule_timezone` directly. The manifest is now the only place a
    # schedule is declared, so there's nothing left to cross-check it against.


def _check_unique_embedding_sources(manifests: dict[str, IntegrationManifest]) -> None:
    """Rule 6 (V4 chunk 3.4): `embedding_sources` strings are globally unique.

    Every source string enqueued/searched/deleted through
    `app.services.embedding.EmbeddingService` is claimed by exactly one
    integration's manifest. Two integrations claiming the same source would
    silently blend unrelated content under one label — a second sync job
    could enqueue over another integration's data, and cross-source search
    would return them as if the same thing.
    """
    claimed_by: dict[str, str] = {}
    for name, manifest in manifests.items():
        for source in manifest.embedding_sources:
            if source in claimed_by:
                raise ManifestValidationError(
                    f"Duplicate embedding source {source!r}: claimed by both "
                    f"{claimed_by[source]!r} and {name!r}"
                )
            claimed_by[source] = name


def _check_unique_tool_names(integrations: dict[str, BaseIntegration]) -> None:
    """Rule 4: tool names from mcp_tools() are unique across all integrations."""
    seen: dict[str, str] = {}
    for name, integration in integrations.items():
        for tool in integration.mcp_tools():
            tool_name = tool["name"]
            if tool_name in seen:
                raise ManifestValidationError(
                    f"Duplicate MCP tool name {tool_name!r}: declared by both "
                    f"{seen[tool_name]!r} and {name!r}"
                )
            seen[tool_name] = name


def validate_manifests(
    manifests: dict[str, IntegrationManifest],
    integrations: dict[str, BaseIntegration] | None = None,
) -> None:
    """Enforce rules 2-5 over `manifests` (rule 1 lives in discover_manifests()).

    `integrations` maps integration name -> live BaseIntegration instance
    (typically `app.integrations.get_all()`, already populated by
    `register_all()`). Checks that need a live instance (name/display_name/
    schedule match, tool-name uniqueness) are skipped for any manifest whose
    name isn't present in `integrations` — this lets tests validate
    hand-built, disk-independent manifest dicts (e.g. to prove a dependency
    cycle raises) without needing a matching real integration.
    """
    integrations = integrations or {}

    _check_dependency_graph(manifests)
    _check_unique_embedding_sources(manifests)

    for name, manifest in manifests.items():
        if manifest.name != name:
            raise ManifestValidationError(
                f"{name}: manifest.name={manifest.name!r} must equal the "
                f"package name {name!r}"
            )
        _check_models(name, manifest)
        integration = integrations.get(name)
        if integration is not None:
            _check_matches_abc(name, manifest, integration)

    if integrations:
        _check_unique_tool_names(integrations)
