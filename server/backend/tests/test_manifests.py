"""Manifest validation tests — V4 chunk 1.1 (unit tier).

Covers:
  - The real tree: discover_manifests() finds all 31 integration manifests
    (30 as of 2026-09-11, plus `alerts` added 2026-09-14 for lios#230 — the
    Alertmanager webhook inlet + reviewable alert log)
    (19 since chunk 1.1, `embedding` since chunk 3.4 turned it into a real
    capability package, `sheets` since chunk 4.2 turned it into a real
    CapabilityService with its own manifest, `notifications` since
    2026-07-31 gave system alerts a push sink, `transcription`
    the same day for audio-to-text, `household` since 2026-08-13 for
    the Domains capability, `google_docs` since 2026-08-20 for reading
    and writing Google Docs, and `solar_forecast` since 2026-08-22 — the
    first `type="deriver"` integration, built on the `app/algo/` harness),
    and validate_manifests()
    passes against the live registered integrations (zero behavior change —
    this is the same tree test_sync_contract.py and test_tool_snapshots.py
    already exercise).
  - Deliberately broken in-memory manifests, each raising
    ManifestValidationError with a clear message: bad model name, unknown
    capability dependency, dependency cycle, duplicate embedding source
    claim (V4 chunk 3.4), duplicate capability claim (V4 chunk 4.2). (A
    schedule-mismatch check used to live here too, cross-checking
    manifest.schedule against the ABC's sync_schedule() — that ABC method
    was deleted in V4 chunk 3.1 once the manifest became the sole source of
    truth for schedule, so there's nothing left to cross-check against.)

V4 chunk 4.2 changed `depends_on` semantics: entries now name *capability
strings* (declared via some manifest's `provides` list — e.g. "mail.query"),
not integration package names directly. `_bare_manifest()` below defaults
`provides=[name]` so pre-4.2 tests that depend on another bare manifest by
its own name keep working unchanged (each bare manifest trivially provides
a capability equal to its own name unless a test overrides `provides`).
"""

import pytest

from app.integrations import INTEGRATIONS, register_all
from app.plugin.manifest import IntegrationManifest
from app.plugin.validate import (
    ManifestValidationError,
    discover_manifests,
    validate_manifests,
)


@pytest.fixture
def real_integrations():
    INTEGRATIONS.clear()
    register_all()
    assert INTEGRATIONS, "expected register_all() to populate the registry"
    return dict(INTEGRATIONS)


def _bare_manifest(name: str, **overrides) -> IntegrationManifest:
    """A minimal, otherwise-valid manifest for `name` — override just the
    field under test so each failure test only exercises one check."""
    defaults = dict(
        name=name,
        display_name=name,
        version="1.0.0",
        type="capability",
        description="test manifest",
        icon="Blocks",
        models=[],
        provides=[name],
        depends_on=[],
    )
    defaults.update(overrides)
    return IntegrationManifest(**defaults)


class TestRealTree:
    def test_discover_manifests_finds_all_31_integrations(self):
        manifests = discover_manifests()
        assert len(manifests) == 31
        for name, manifest in manifests.items():
            assert manifest.name == name

    def test_the_deriver_type_is_declared_and_owns_no_tables(self):
        """`solar_forecast` is the reference deriver. `models=[]` is the load-
        bearing part: predictions live in the shared kernel tables, which is why
        adding another forecaster needs no migration."""
        manifests = discover_manifests()
        derivers = {n: m for n, m in manifests.items() if m.type == "deriver"}
        assert "solar_forecast" in derivers
        for name, manifest in derivers.items():
            assert manifest.models == [], f"{name}: a deriver owns no tables"

    def test_discover_manifests_includes_sheets(self):
        # sheets became a real CapabilityService in V4 chunk 4.2 (manifest,
        # provides=["sheets.write"]) — no longer a manifest-less library.
        manifests = discover_manifests()
        assert "sheets" in manifests
        assert "sheets.write" in manifests["sheets"].provides

    def test_validate_manifests_passes_on_real_tree(self, real_integrations):
        manifests = discover_manifests()
        # Should not raise.
        validate_manifests(manifests, real_integrations)

    def test_every_manifest_name_matches_a_registered_integration(self, real_integrations):
        manifests = discover_manifests()
        assert set(manifests) == set(real_integrations)


class TestBrokenManifests:
    def test_bad_model_name_raises(self, real_integrations):
        manifests = {
            "google_calendar": _bare_manifest(
                "google_calendar", models=["NotARealModelClass"]
            ),
        }
        with pytest.raises(ManifestValidationError, match="NotARealModelClass"):
            validate_manifests(manifests, real_integrations)

    def test_unknown_dependency_raises(self):
        manifests = {
            "widget": _bare_manifest("widget", depends_on=["does_not_exist"]),
        }
        with pytest.raises(ManifestValidationError, match="does_not_exist"):
            validate_manifests(manifests)

    def test_unresolvable_capability_dependency_fails_boot(self):
        # V4 chunk 4.2: depends_on names a capability string. If no
        # manifest's `provides` declares it, boot must fail loudly — this is
        # the acceptance criterion "removing a capability provider fails
        # startup for its dependents".
        manifests = {
            "widget": _bare_manifest("widget", provides=[], depends_on=["some.capability"]),
        }
        with pytest.raises(ManifestValidationError, match="unknown capability 'some.capability'"):
            validate_manifests(manifests)

    def test_duplicate_capability_raises(self):
        manifests = {
            "widget_a": _bare_manifest("widget_a", provides=["shared.capability"]),
            "widget_b": _bare_manifest("widget_b", provides=["shared.capability"]),
        }
        with pytest.raises(ManifestValidationError, match="Duplicate capability"):
            validate_manifests(manifests)

    def test_dependency_cycle_raises(self):
        manifests = {
            "a": _bare_manifest("a", depends_on=["b"]),
            "b": _bare_manifest("b", depends_on=["c"]),
            "c": _bare_manifest("c", depends_on=["a"]),
        }
        with pytest.raises(ManifestValidationError, match="cycle"):
            validate_manifests(manifests)

    def test_name_mismatch_raises(self):
        # Manifest's own `.name` field must equal its dict key.
        manifests = {
            "widget": _bare_manifest("not-widget"),
        }
        with pytest.raises(ManifestValidationError, match="widget"):
            validate_manifests(manifests)

    def test_duplicate_embedding_source_raises(self):
        manifests = {
            "widget_a": _bare_manifest("widget_a", embedding_sources=["vault"]),
            "widget_b": _bare_manifest("widget_b", embedding_sources=["vault"]),
        }
        with pytest.raises(ManifestValidationError, match="Duplicate embedding source"):
            validate_manifests(manifests)

    def test_duplicate_tool_name_raises(self, real_integrations):
        class _FakeIntegration:
            name = "fake_dup"

            def mcp_tools(self):
                # Collide with a real tool name already in the registry.
                # Not every integration registers a tool (`alerts`, `sheets`,
                # `transcription`, `vision` are pure facade providers with
                # `mcp_tools() == []` — see their own manifests/__init__.py),
                # so this picks the first one that actually has a tool
                # rather than assuming dict-iteration order lands on one.
                any_real_name = next(
                    tools[0]["name"]
                    for integration in real_integrations.values()
                    if (tools := integration.mcp_tools())
                )
                return [{"name": any_real_name}]

        manifests = discover_manifests()
        integrations = dict(real_integrations)
        integrations["fake_dup"] = _FakeIntegration()
        # fake_dup has no manifest, but that's fine — validate_manifests only
        # needs the integrations dict for the tool-uniqueness sweep, and the
        # manifests dict already covers all real names.
        with pytest.raises(ManifestValidationError, match="Duplicate MCP tool name"):
            validate_manifests(manifests, integrations)
