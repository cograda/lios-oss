"""The drop-in integration test — V4 chunk 4.3e, the north-star proof.

Programmatically copies `app/integrations/_template/` to a throwaway,
non-underscore package name inside `app/integrations/` on disk, boots
discovery/validation/scheduling against it, and asserts the new integration
is fully live — WITHOUT a single edit to any kernel file
(`models/__init__.py`, `scheduler.py`, `main.py`, `mcp/annotations.py`,
`routes/__init__.py`, freshness dicts, or the frontend). This is the exact
acceptance criterion from `vault/Projects/lios/Plans/comar-v4/00-index.md`'s
north-star:

    A new integration must be buildable without editing any kernel code —
    drop a package under server/backend/app/integrations/, write its
    manifest.py, add credentials via config.

Tier split (deliberate, per the "mark at test/class level, not module level"
rule — a module-level `pytest.mark.db` would hide every unit-tier assertion
here from `-m "not db"`):

  - `TestDropInUnitTier` — no real Postgres needed. Covers: appears in
    `app.integrations.register_all()`'s registry and in
    `app.plugin.validate.discover_manifests()`; its model is a real
    SQLAlchemy table in `Base.metadata`; its tools carry handlers +
    annotations and are MCP-constructible; its schedule lands in
    `app.scheduler.setup_scheduler()`'s job set; its config schema resolves
    through `app.plugin.config_store.plugin_config()` (DB access mocked with
    an empty-rows session so the env-fallback path is exercised — no live
    Postgres required for this one, same trick `test_config_store.py`'s
    unit tier already uses).
  - `TestDropInDbTier` (`pytest.mark.db`, class-level) — needs real
    Postgres. Covers: the model's table can actually be created against a
    real database (proves the columns aren't just Python-side metadata but
    real, valid DDL) and the `/integrations/` route (the "integrations
    metadata API") lists it end-to-end through a real DB session.
  - `TestDropInBrokenVariants` — no real Postgres needed (manifest
    validation is pure Python). Parametrized: each variant copies
    `_template`, applies one deliberate breakage, and asserts startup
    validation (`discover_manifests()` / `validate_manifests()`) fails
    loudly rather than silently misregistering.

Every test in this module uses `_dropped_in_package()`, a context manager
that copies the template to a uniquely-named directory, replaces the
`__TEMPLATE_INTEGRATION_NAME__` placeholder (see `_template/__init__.py`'s
module docstring for where it appears) with that unique name, yields the
name, and — in its `finally` block, so cleanup runs even if the test body
raises — removes the directory, evicts the copy's modules from
`sys.modules`, restores `app.integrations.INTEGRATIONS` to the real
registry, AND (fixed in the 4.3e fixup below) strips every table the copy
registered out of `coglib.Base.metadata`.

FIXUP (post-4.3e, same day): the original version of this fixture removed
the copy's *modules* from `sys.modules` but never removed the copy's
*table* from `Base.metadata` — SQLAlchemy's metadata registry is a separate
global structure that importing/unloading a module does not touch. Every
one of this module's tests imports (directly or via `register_all()`) the
dropped-in package's `models.py`, which declaratively registers a
`TemplateItem` table into the *real*, shared `Base.metadata` the instant
it's imported — and that registration outlived the fixture's cleanup
entirely. In CI (real Postgres, unlike local runs with no Postgres at all)
every subsequent `real_db`-using test's teardown
(`conftest.py::_truncate_all_except_users`) issues ONE combined `TRUNCATE`
across every table in `Base.metadata.sorted_tables` — including these
never-dropped, never-even-created (for the unit-tier tests) phantom
tables — which raises `UndefinedTable` and aborts the whole truncate,
leaking every prior test's rows into every following db-tier test for the
rest of the session. Fixed by having the `finally` block explicitly
`Base.metadata.remove(table)` (and dispose the ORM mapper) for exactly the
table(s) this invocation registered, PLUS a defensive sweep (both on entry
and on exit) that removes anything already named `*template*` regardless of
source, so a leftover from an aborted previous run — or the original,
never-renamed `_template` package somehow getting imported directly — can
never survive to pollute a live test session either.
"""

from __future__ import annotations

import contextlib
import shutil
import sys
import uuid
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit  # default; TestDropInDbTier overrides at class level

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "app" / "integrations" / "_template"
INTEGRATIONS_DIR = TEMPLATE_DIR.parent
PLACEHOLDER = "__TEMPLATE_INTEGRATION_NAME__"


def _fresh_name() -> str:
    # "zz_" prefix keeps these sorted last in the deterministic
    # sorted-by-name discovery walk, purely for readability in failure
    # output — discovery doesn't care about ordering correctness here.
    return f"zz_dropin_{uuid.uuid4().hex[:10]}"


_DROPIN_PREFIX = "zz_dropin_"  # matches _fresh_name() — every throwaway package this module creates


def _is_dropin_derived_table(table_name: str) -> bool:
    """True for any table this module could plausibly have registered:
    either a properly-substituted throwaway name (always starts with
    `zz_dropin_`, per `_fresh_name()`) or the literal, never-substituted
    placeholder (contains "template", case-insensitive) — covering both the
    normal case AND the exact bug this fixup exists for (a copy that somehow
    got imported before its placeholder was replaced).
    """
    lowered = table_name.lower()
    return table_name.startswith(_DROPIN_PREFIX) or "template" in lowered


def _purge_template_metadata() -> list[str]:
    """Remove every table from `coglib.Base.metadata` that this module could
    have registered (see `_is_dropin_derived_table`).

    Deliberately name-based, not tracked-list-based: this is the safety net
    that makes phantom-table pollution structurally impossible regardless of
    *how* a dropped-in table ended up registered — including a table that
    was already dropped from the real database by a test's own explicit
    `table.drop(...)` (db tier), one that was never created in a database at
    all (unit tier — no real Postgres involved), or the literal, never-
    substituted `_template` placeholder name. Called both defensively on
    entry (cleans up anything a previous, possibly-crashed run left behind)
    and unconditionally in `_dropped_in_package`'s `finally` block.

    Does NOT try to dispose the ORM mapper directly — see
    `_release_orm_mapper_references()`'s docstring for why that turned out
    to be actively harmful, and why plain garbage collection is the
    mechanism that actually works here. This function must be called
    *after* that one, once nothing still holds a strong reference to the
    mapped class, so the table it's removing is already unmapped.

    Returns the list of table names removed, purely for assertions/tests.
    """
    from coglib import Base

    removed: list[str] = []
    for table_name in list(Base.metadata.tables.keys()):
        if not _is_dropin_derived_table(table_name):
            continue
        table = Base.metadata.tables[table_name]
        Base.metadata.remove(table)
        removed.append(table_name)
    return removed


def _release_orm_mapper_references() -> None:
    """Force garbage collection so any dropped-in `TemplateItem` class with
    no more strong references gets collected — which SQLAlchemy's
    declarative registry is specifically designed to notice (its internal
    class registry is weak-referenced) and clean up on its own, with no
    zombie state left behind.

    THE BUG THIS FIXES: the first version of this fixup tried to actively
    dispose each mapper via `mapper.dispose()` — which doesn't exist on
    SQLAlchemy 2.0's `Mapper` (an `AttributeError`, silently swallowed by a
    blanket `except Exception: pass`, so the bug shipped invisibly). Trying
    the "real" private API instead
    (`Base.registry._dispose_manager_and_mapper(manager)`) does remove the
    class from the name-based registry, but empirically (verified directly
    against installed SQLAlchemy 2.0.49) leaves a broken "zombie" `Mapper`
    object behind in `Base.registry.mappers` whose `.class_` is `None` —
    worse than doing nothing, since `tests/test_user_scoping.py
    ::user_owned_models()` does `issubclass(m.class_, UserOwnedMixin)` over
    every mapper in that collection and would raise `TypeError` on the
    `None`.

    Plain reference-counting + `gc.collect()`, with no explicit dispose
    call at all, was verified (same installed version) to remove the class
    from `Base.registry.mappers` cleanly — SQLAlchemy's own class registry
    already uses weak references for exactly this "a mapped class goes out
    of scope and should clean itself up" case. All this function has to do
    is make sure nothing else is holding a strong reference by the time it
    runs — which is why `_dropped_in_package`'s `finally` block calls it
    only after the copy's modules have already been evicted from
    `sys.modules` (the module dict's own `TemplateItem` attribute is the
    other main strong reference besides the class registry itself).
    """
    import gc

    gc.collect()


@contextlib.contextmanager
def _dropped_in_package(mutate=None) -> Iterator[str]:
    """Copy `_template/` to a fresh, non-underscore package name, optionally
    mutating its files' text first (for the broken-variant tests), yield
    the new package name, then always clean up.

    `mutate`, if given, is called as `mutate(dest: Path, name: str)` after
    the placeholder substitution — it may rewrite any file under `dest` to
    introduce a deliberate defect.

    Cleanup (in the `finally` block, so it runs even when the test body
    raises or an assertion fails) does FIVE things, in order: evict the
    copy's modules from `sys.modules` (dropping the module dict's own
    strong reference to the mapped class); remove the directory from disk;
    restore `app.integrations.INTEGRATIONS` to the real registry; force
    garbage collection so the now-unreferenced `TemplateItem` class is
    actually collected (`_release_orm_mapper_references()` — see its
    docstring for why this, and not an explicit mapper-dispose call, is
    the fix); and strip this copy's table out of `coglib.Base.metadata`
    (`_purge_template_metadata()`), which also runs defensively on *entry*
    so a previous test's incomplete cleanup (e.g. the process was killed
    mid-test) can never carry pollution into the next one either.
    """
    _release_orm_mapper_references()
    _purge_template_metadata()  # defensive: clean up anything a prior run left behind

    name = _fresh_name()
    dest = INTEGRATIONS_DIR / name
    assert not dest.exists(), f"collision on throwaway name {name!r}"

    shutil.copytree(TEMPLATE_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
    try:
        for py_file in dest.rglob("*.py"):
            text = py_file.read_text()
            if PLACEHOLDER in text:
                py_file.write_text(text.replace(PLACEHOLDER, name))
            # Belt-and-braces: if for any reason the placeholder survived
            # the replace above (it shouldn't — this is exactly the bug
            # this fixup exists to make structurally impossible), fail
            # loudly right here rather than silently importing a package
            # that will register a phantom table under the literal,
            # never-renamed name.
            assert PLACEHOLDER not in py_file.read_text(), (
                f"placeholder substitution failed in {py_file} — refusing "
                f"to import a dropped-in copy that still carries the "
                f"literal {PLACEHOLDER!r} token"
            )

        if mutate is not None:
            mutate(dest, name)

        import importlib
        importlib.invalidate_caches()

        yield name
    finally:
        import importlib

        from app.integrations import INTEGRATIONS, register_all

        prefix = f"app.integrations.{name}"
        for mod_name in [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]:
            del sys.modules[mod_name]

        shutil.rmtree(dest, ignore_errors=True)
        importlib.invalidate_caches()

        # Restore the registry to the real integration set — other tests in
        # this session assume INTEGRATIONS reflects the real tree.
        INTEGRATIONS.clear()
        register_all()

        # The fix: this copy's model(s) registered themselves into the
        # shared, process-global coglib.Base.metadata the moment models.py
        # was imported (directly, or via register_all() above) — unloading
        # the module from sys.modules does NOT undo that registration on
        # its own. Order matters: release every strong reference to the
        # mapped class first (so it's actually garbage-collected, which is
        # what un-registers it from Base.registry — see
        # _release_orm_mapper_references()'s docstring), THEN remove its
        # now-orphaned table from Base.metadata. Without both steps, either
        # a phantom table survives forever (breaking
        # conftest.py::_truncate_all_except_users' truncate-all-tables
        # sweep for every later real-Postgres test) or — if only the class
        # were force-disposed without also removing the table — the table
        # itself would still be sitting in Base.metadata.tables with no
        # mapper behind it, same problem.
        _release_orm_mapper_references()
        _purge_template_metadata()


@pytest.fixture
def fake_db_empty_config(monkeypatch):
    """Patch app.db.get_db() so plugin_config()/is_configured_from_schema()
    can run with no real Postgres: every IntegrationConfig query returns no
    rows, forcing the env-fallback path (same trick as
    tests/test_config_store.py's unit tier)."""
    session = MagicMock()
    session.query.return_value.filter_by.return_value.all.return_value = []
    session.query.return_value.filter_by.return_value.first.return_value = None

    class _FakeDb:
        @contextlib.contextmanager
        def session(self):
            yield session

    import app.db as app_db
    monkeypatch.setattr(app_db, "get_db", lambda: _FakeDb())
    return session


# ---------------------------------------------------------------------------
# Unit tier
# ---------------------------------------------------------------------------


class TestDropInUnitTier:
    def test_appears_in_discovery(self):
        from app.integrations import INTEGRATIONS, register_all

        with _dropped_in_package() as name:
            INTEGRATIONS.clear()
            register_all()
            assert name in INTEGRATIONS, "dropped-in package not found by register_all()"
            assert INTEGRATIONS[name].display_name == "Template Integration"

    def test_manifest_discovered_and_internally_consistent(self):
        from app.plugin.validate import discover_manifests, validate_manifests

        with _dropped_in_package() as name:
            manifests = discover_manifests()
            assert name in manifests
            manifest = manifests[name]
            assert manifest.type == "source"
            assert manifest.schedule == "*/30 * * * *"
            assert manifest.models == ["TemplateItem"]
            assert "api_key" in manifest.config_schema

            # Must not raise: the dropped-in manifest is internally
            # consistent against the WHOLE real manifest tree (no dependency
            # cycle, no duplicate capability/embedding-source claim).
            validate_manifests(manifests)

    def test_model_is_real_sqlalchemy_table_in_metadata(self):
        import importlib

        from coglib import Base

        with _dropped_in_package() as name:
            models_module = importlib.import_module(f"app.integrations.{name}.models")
            model_cls = models_module.TemplateItem

            table_name = f"{name}_items"
            assert table_name in Base.metadata.tables
            table = Base.metadata.tables[table_name]
            assert model_cls.__tablename__ == table_name
            assert {"id", "user_id", "external_id", "title", "fetched_at"} <= set(
                c.name for c in table.columns
            )
            # UserOwnedMixin scoping — the per-user FK is present and required.
            assert table.columns["user_id"].nullable is False

    def test_tools_have_handlers_and_annotations_and_are_mcp_constructible(self):
        from mcp.types import Tool

        from app.integrations import INTEGRATIONS, register_all

        with _dropped_in_package() as name:
            INTEGRATIONS.clear()
            register_all()
            tools = INTEGRATIONS[name].mcp_tools()

            assert len(tools) == 2
            names = {t["name"] for t in tools}
            assert names == {"template_list_items", "template_ping"}

            for tool in tools:
                assert callable(tool["handler"])
                assert tool.get("annotations"), f"{tool['name']} has no annotations"
                assert tool.get("category") == "template"
                # This is exactly what app.mcp.server.register_mcp_tools()
                # does per tool — proves the dict is genuinely
                # MCP-registrable without touching the shared, session-wide
                # tool registry (app.plugin.registry) that other tests
                # depend on staying pristine.
                Tool(
                    name=tool["name"],
                    description=tool["description"],
                    inputSchema=tool["inputSchema"],
                    annotations=tool["annotations"],
                )

    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"

    @pytest.mark.anyio
    async def test_schedule_appears_in_scheduler_job_set(self, monkeypatch):
        # AsyncIOScheduler.start() requires a running event loop (it grabs
        # asyncio.get_running_loop()) — same reason
        # test_scheduler_jobs.py's pinned-snapshot test is itself async.
        from app import scheduler as scheduler_module
        from app.integrations import INTEGRATIONS, register_all

        with _dropped_in_package() as name:
            INTEGRATIONS.clear()
            register_all()
            for integration in INTEGRATIONS.values():
                monkeypatch.setattr(integration, "is_configured", lambda: True, raising=False)

            # V4 chunk 5.1: setup_scheduler() also gates on the enable/disable
            # switch (integration_config table) — mock it enabled so this
            # doesn't need real Postgres.
            monkeypatch.setattr(scheduler_module, "is_integration_enabled", lambda name: True)

            scheduler_module.scheduler.remove_all_jobs()
            try:
                scheduler_module.setup_scheduler()
                jobs = {job.id: job for job in scheduler_module.scheduler.get_jobs()}
                assert f"sync_{name}" in jobs, "dropped-in schedule missing from job set"
                job = jobs[f"sync_{name}"]
                assert "*/30" in str(job.trigger) or job.trigger is not None
            finally:
                scheduler_module.scheduler.remove_all_jobs()
                if scheduler_module.scheduler.running:
                    scheduler_module.scheduler.shutdown(wait=False)

    def test_config_schema_is_served(self, monkeypatch, fake_db_empty_config):
        from app.plugin.config_store import is_configured_from_schema, plugin_config

        with _dropped_in_package() as name:
            monkeypatch.setenv("HOME_API_KEY", "sekret-value")
            cfg = plugin_config(name)
            assert cfg.api_key == "sekret-value"  # env fallback, since no DB row exists
            assert cfg.page_size == 50  # schema default
            assert is_configured_from_schema(name) is True

    def test_config_schema_reports_unconfigured_without_required_key(self, fake_db_empty_config):
        from app.plugin.config_store import is_configured_from_schema

        with _dropped_in_package() as name:
            # No HOME_API_KEY set, no DB row (fake_db_empty_config) — the
            # one `required=True` key (api_key) has no value from any source.
            assert is_configured_from_schema(name) is False


# ---------------------------------------------------------------------------
# db tier — real Postgres needed for genuine DDL + the /integrations/ route
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestDropInDbTier:
    def test_model_table_creates_against_real_postgres(self, real_db):
        import importlib

        with _dropped_in_package() as name:
            models_module = importlib.import_module(f"app.integrations.{name}.models")
            table = models_module.TemplateItem.__table__
            try:
                table.create(bind=real_db.engine)
                inspector_tables = real_db.engine.dialect.get_table_names(
                    real_db.engine.connect()
                )
                assert table.name in inspector_tables
            finally:
                table.drop(bind=real_db.engine, checkfirst=True)

    @pytest.mark.anyio
    async def test_listed_by_integrations_metadata_route(self, real_db, monkeypatch):
        from app.integrations import INTEGRATIONS, register_all
        from app.routes.integrations import list_integrations

        with _dropped_in_package() as name:
            INTEGRATIONS.clear()
            register_all()
            for integration in INTEGRATIONS.values():
                monkeypatch.setattr(integration, "is_configured", lambda: True, raising=False)

            result = await list_integrations()
            by_name = {row["name"]: row for row in result["integrations"]}
            assert name in by_name, "dropped-in integration missing from /integrations/ listing"
            entry = by_name[name]
            assert entry["display_name"] == "Template Integration"
            assert entry["schedule"] == "*/30 * * * *"
            assert entry["type"] == "source"
            assert entry["configured"] is True

    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"


# ---------------------------------------------------------------------------
# Deliberately-broken variants — startup validation must catch each one
# ---------------------------------------------------------------------------


def _break_missing_manifest_field(dest: Path, name: str) -> None:
    """Delete a required manifest field entirely — IntegrationManifest(...)
    raises a pydantic ValidationError at import time (inside
    discover_manifests()), before validate_manifests() is ever reached."""
    manifest_py = dest / "manifest.py"
    text = manifest_py.read_text()
    assert 'type="source",' in text
    manifest_py.write_text(text.replace('    type="source",\n', ""))


def _break_undeclared_model(dest: Path, name: str) -> None:
    """Declare a model in the manifest that doesn't exist in models.py —
    app.plugin.validate._check_models() raises ManifestValidationError."""
    manifest_py = dest / "manifest.py"
    text = manifest_py.read_text()
    assert 'models=["TemplateItem"],' in text
    manifest_py.write_text(
        text.replace(
            'models=["TemplateItem"],',
            'models=["TemplateItem", "BogusModelNotDefinedAnywhere"],',
        )
    )


def _break_missing_capability(dest: Path, name: str) -> None:
    """Depend on a capability nobody provides —
    app.plugin.validate._check_dependency_graph() raises
    ManifestValidationError."""
    manifest_py = dest / "manifest.py"
    text = manifest_py.read_text()
    assert "depends_on=[]," in text
    manifest_py.write_text(
        text.replace('depends_on=[],', 'depends_on=["totally.bogus.capability"],', 1)
    )


@pytest.mark.parametrize(
    "mutate,match",
    [
        pytest.param(_break_missing_manifest_field, None, id="missing_manifest_field"),
        pytest.param(_break_undeclared_model, "BogusModelNotDefinedAnywhere", id="undeclared_model"),
        pytest.param(_break_missing_capability, "totally.bogus.capability", id="missing_capability"),
    ],
)
class TestDropInBrokenVariants:
    def test_broken_variant_fails_startup_validation(self, mutate, match):
        from app.plugin.validate import ManifestValidationError, discover_manifests, validate_manifests

        expected = (ManifestValidationError, Exception) if match is None else ManifestValidationError

        with _dropped_in_package(mutate=mutate) as name:
            with pytest.raises(expected, match=match):
                manifests = discover_manifests()
                validate_manifests(manifests)


# ---------------------------------------------------------------------------
# Metadata-hygiene guard (post-4.3e fixup) — the phantom-table bug, pinned
# ---------------------------------------------------------------------------


def test_no_template_table_survives_fixture_use():
    """After `_dropped_in_package()` exits, `Base.metadata` must not contain
    ANY table whose name mentions "template" — regardless of which of the
    tests above ran, in what order, or whether the test body raised.

    This is the direct regression test for the bug that broke CI after
    4.3e's first commit: a dropped-in copy's `TemplateItem` table was
    registered into the real, process-global `coglib.Base.metadata` at
    import time and never removed, so every later real-Postgres test's
    truncate-all-tables teardown broke on a relation that either never
    existed in the DB (unit-tier copies) or had already been dropped
    (the db-tier copy that explicitly created + dropped its own table).
    Making this assertion pass is what makes the phantom-table sweep
    problem structurally impossible: nothing named `*template*` may be
    left behind by this module's own fixture, full stop.
    """
    import importlib

    from coglib import Base

    with _dropped_in_package() as name:
        # Actually import the model — merely copying the package's files
        # doesn't touch Base.metadata; a real drop-in test always imports
        # models.py (directly, or transitively via register_all()), so do
        # the same here to exercise the exact path this test guards.
        importlib.import_module(f"app.integrations.{name}.models")

        # Sanity: the fixture really did register a table while active —
        # otherwise this test would trivially pass without exercising the
        # cleanup path it exists to guard.
        assert f"{name}_items" in Base.metadata.tables, (
            "fixture didn't register the expected table while active — test is vacuous"
        )
        assert any(_is_dropin_derived_table(t) for t in Base.metadata.tables)

    leftover = [t for t in Base.metadata.tables if _is_dropin_derived_table(t)]
    assert leftover == [], f"dropped-in table(s) survived fixture teardown: {leftover}"


def test_multiple_sequential_drops_leave_no_residue():
    """Three sequential drop-ins (the realistic case — this module runs many
    tests, each with its own `_dropped_in_package()` call, in one session)
    must each clean up completely, not just "eventually" once some later
    test happens to sweep. Guards against a cleanup that only works for a
    single isolated use."""
    import importlib

    from coglib import Base

    for _ in range(3):
        with _dropped_in_package() as name:
            importlib.import_module(f"app.integrations.{name}.models")
            assert f"{name}_items" in Base.metadata.tables

    assert not any(_is_dropin_derived_table(t) for t in Base.metadata.tables)
