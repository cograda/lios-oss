"""Shared test fixtures for server tests.

Two tiers, selected by marker:

  unit  (default) — no database, no external services; DB interactions
        mocked. Fast and infrastructure-free. Everything not explicitly
        marked `db` gets this marker automatically.
  db    — real Postgres (pgvector). Uses LIOS_TEST_DATABASE_URL (or the pre-rename COMAR_TEST_DATABASE_URL) if set
        (CI service container), else spins up a throwaway
        pgvector/pgvector:pg16 via testcontainers (needs Docker locally).
        Schema is built exactly the way production builds a fresh DB
        (app.db._run_migrations: create_tables + alembic stamp head).

Run only the fast tier with `pytest -m "not db"`.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add backend to sys.path so `from app.xxx import ...` works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Add coglib from alexunism monorepo (shared package — installed in Docker,
# but needs path hint for local pytest runs)
_coglib_candidates = [
    Path.home() / "Desktop" / "Code" / "alexunism" / "coglib" / "src",
    Path(__file__).resolve().parent.parent / "coglib" / "src",  # if synced locally
    Path(__file__).resolve().parent.parent / "coglib",  # flat layout
]
for _candidate in _coglib_candidates:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
        break


# Pre-import the real `app` package so individual tests that do
# `sys.modules.setdefault("app", ModuleType("app"))` don't shadow it with
# a non-package stub. Several test files stub `app` to dodge fastapi/SQLAlchemy
# imports in app.db; without this, the first such test poisons sys.modules and
# any later test that does `from app.auth.utils import ...` fails with
# "'app' is not a package".
import importlib  # noqa: E402

importlib.import_module("app")


_TEST_ENCRYPTION_KEY = None


@pytest.fixture(autouse=True)
def _default_encryption_key(monkeypatch):
    """Give every test a working Fernet key by default.

    V4 chunk 3.3 made `app.auth.encryption` fail-closed: encrypt/decrypt
    raises if `HOME_OAUTH_ENCRYPTION_KEY` is unset. Without this fixture,
    every test that touches an OAuthToken or a secret `integration_config`
    value would need to set a key itself just to collect/run. Tests that
    specifically want the "key unset" fail-closed path (test_encryption.py)
    override this within the test via their own patch/monkeypatch, which
    layers on top of (and is torn down before) this one.
    """
    global _TEST_ENCRYPTION_KEY
    if _TEST_ENCRYPTION_KEY is None:
        from cryptography.fernet import Fernet
        _TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()

    from app.config import settings
    monkeypatch.setattr(settings, "oauth_encryption_key", _TEST_ENCRYPTION_KEY)
    yield


@pytest.fixture(autouse=True)
def _pin_test_user():
    """Bind user_id=1 for every test by default.

    Production code now requires `current_user_id()` callers to be inside a
    `use_user(...)` block — there is no silent default. Tests that exercise
    handlers directly (without going through the v1 / MCP entrypoints) need
    the binding too. Tests that explicitly want to verify the unbound state
    can override with their own `pytest.raises(RuntimeError)` block inside
    a `with use_user(0):` (sentinel) or by reading the ContextVar directly.
    """
    try:
        from app.auth.context import use_user
    except Exception:
        # Some tests stub `app.auth.context` — they manage their own ContextVar.
        yield
        return
    with use_user(1):
        yield


@pytest.fixture(autouse=True)
def _isolated_integration_registry():
    """Snapshot and restore `app.integrations.INTEGRATIONS` around every test.

    A baker's dozen test files replace the whole registry via
    `INTEGRATIONS.clear(); register_all()` (test_drop_in_integration.py,
    test_plugin_discovery.py, test_algo_harness.py, test_manifests.py,
    test_sync_contract.py, test_user_scoping.py, test_personalisation_guard.py,
    test_alerts_schedule_aware_staleness.py, test_scheduler_jobs.py,
    test_capability_boundaries.py, test_state_of_project.py,
    test_kernel_import_guard.py — plus test_scheduler.py, which mutates one
    entry directly with `INTEGRATIONS[name] = integration`) to exercise
    discovery against a real or fabricated `app/integrations/` tree, with no
    fixture reverting any of it afterward. That left every later test seeing
    whichever registry state the last mutator happened to leave behind —
    order-dependent, and fatal the moment tests stop running in guaranteed
    file order (pytest-xdist's worker scheduling interleaves tests from
    different files non-deterministically; see the Wave 5.4 PR body for the
    measured 0-8 different failures per xdist run this caused before this
    fixture existed).

    `INTEGRATIONS` is a single, never-reassigned module-level dict (see
    `app/integrations/__init__.py` — every mutator above imports and clears/
    updates the *same* object, never rebinds the name), so an in-place
    clear()+update() restores it exactly regardless of what a test replaced
    it with or which test ran first or last.
    """
    from app.integrations import INTEGRATIONS

    before = dict(INTEGRATIONS)
    yield
    INTEGRATIONS.clear()
    INTEGRATIONS.update(before)


@pytest.fixture(autouse=True)
def _no_real_db_outside_db_tier(request, monkeypatch):
    """Unit-tier tests must never reach the real Postgres pinned for the
    db-tier session — see `test_db`'s docstring for why `app.db._db` is
    pinned at *session* scope rather than reverted per-test (a background
    thread racing a `None` gap, not a hypothetical).

    That session-wide pin is exactly what made `test_notifications.py`'s
    `TestConfigEnforcement` (an unmarked, "unit"-tier class — no `db_session`/
    `real_db` in sight) corrupt later `@pytest.mark.db` tests **only when run
    in the same pytest process** as a db-marked test, which is precisely what
    CI's `server-unit`/`server-db` split (2026-09-05, PR #87) never does
    (they're two separate `pytest` invocations, so `app.db._db` is never
    pinned in the unit job at all) but a full local `pytest tests/` — or this
    file run whole — does. Root cause: `TestConfigEnforcement` calls the
    *real* `app.integrations.notifications.client.publish()` to assert it
    raises `NotifyConfigError`, but never stubs `_record_send` the way its
    sibling `TestPublishRouting` does (that class's `_wire()` explicitly
    monkeypatches it, with a comment explaining exactly this hazard). Because
    `_record_send`'s `get_db()` resolves to whatever `app.db._db` is pinned
    to, and `real_db` had already pinned it to the live test-container
    Postgres by the time this class ran, the ledger write inside `publish()`'s
    except-branch executed for real — a genuine, uncommitted-by-any-fixture
    `INSERT ... COMMIT` against the shared container, since this test
    requests no `real_db`/`db_session` fixture to roll anything back. That
    row then permanently occupies id=1 (or whatever the sequence was reset
    to), so the next `@pytest.mark.db` test's own insert hits a real
    `UniqueViolation` — reproduced and bisected down to exactly these three
    tests for the Wave 5.4 PR.

    Rather than patch every test that forgets to stub a ledger write (an
    unbounded, easy-to-reintroduce category), this fixture makes the mistake
    unable to reach live data in the first place: for any test not marked
    `db`, `app.db._db` is monkeypatched to `None` for the test's duration
    (reverted automatically at teardown, restoring whatever `test_db` pinned
    it to — this never disturbs an actual `@pytest.mark.db` test, which
    always carries the marker). A `get_db()` call from inside a unit test
    then either connects to nothing (this local/CI environment has no
    reachable production Postgres) or lazily builds a throwaway `Database()`
    from production settings that fails to connect — both cases raise, and
    every current call site that reaches `get_db()` from a best-effort path
    (`_record_send`, `_update_sync_state`, `_record_push_channel_health`) already
    catches and swallows exactly that kind of exception, so behavior is
    unchanged for a correctly-isolated unit test and merely fails closed
    (instead of corrupting shared state) for one that isn't.
    """
    if "db" not in request.keywords:
        import app.db as app_db

        monkeypatch.setattr(app_db, "_db", None)
    yield


@pytest.fixture
def mock_session():
    """A mock SQLAlchemy session for unit tests."""
    session = MagicMock()
    session.query.return_value = session._query_mock
    session._query_mock.filter_by.return_value = session._query_mock
    session._query_mock.filter.return_value = session._query_mock
    session._query_mock.first.return_value = None
    session._query_mock.scalar.return_value = 0
    session._query_mock.all.return_value = []
    return session


# ---------------------------------------------------------------------------
# Real-Postgres tier (`db` marker)
# ---------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line(
        "markers", "unit: fast tests, mocked DB (auto-applied to unmarked tests)"
    )
    config.addinivalue_line(
        "markers",
        "integrations_tree: writes or scans app/integrations/ on disk — run serially, "
        "never interleaved with each other under xdist (auto-applied by file, see conftest)",
    )
    config.addinivalue_line(
        "markers", "db: requires real Postgres (testcontainers or COMAR_TEST_DATABASE_URL)"
    )
    config.addinivalue_line(
        "markers",
        "route_reachability: Wave 5.10 CI hygiene route-reachability check — "
        "run as its own named CI step (core-tests.yml's server-unit job) so "
        "a failure names itself instead of being buried in ~1,400 other "
        "unit-tier results; excluded from the main unit-tier run for the "
        "same reason.",
    )


# Fixtures that can only be satisfied by the real test Postgres. A test that
# requests one of these IS a db-tier test whether or not its author remembered
# `@pytest.mark.db` — and forgetting is exactly what happened: on 2026-09-05
# `TestPerUserRouting` (test_notifications.py) requested `db_session` unmarked,
# so the unit job (no Postgres service, `-m "not db"`) collected it, testcontainers
# obligingly started a Postgres on the CI runner's Docker, and with `-n auto`
# four workers raced `create_all` on one fresh container and died with
# `DuplicateTable`. It had passed on `main` by luck of scheduling. Deriving the
# marker from the fixtures makes the class of mistake impossible rather than
# fixing the one instance.
DB_FIXTURES = frozenset({
    "test_db", "real_db", "real_db_concurrent", "db_session", "db_session_concurrent",
})


# Files that WRITE a temporary drop-in package into app/integrations/ (to prove
# real discovery) and files that ENUMERATE that directory. Under xdist these ran
# on different workers, so a scan could see a package mid-creation or
# mid-deletion: `test_plugin_discovery` compared two register_all() sets that
# differed by one transient package, `test_capability_boundaries` read an
# __init__.py that had just been removed, and `test_algo_harness` imported a
# `zz_dropin_*.tools` that another worker had already deleted. Twice in one
# day (2026-09-06) that failed the main image build after the PR's own run
# had passed. Pinning both sides to one xdist group (`--dist loadgroup` in
# core-tests.yml) runs them on a single worker, in order; everything else
# stays parallel.
INTEGRATIONS_TREE_FILES = frozenset({
    # writers
    "test_algo_harness.py", "test_drop_in_integration.py", "test_lastfm_client.py",
    "test_lastfm_sync.py", "test_reminders_commands.py", "test_state_of_project.py",
    "test_vault_watcher.py",
    # scanners
    "test_plugin_discovery.py", "test_capability_boundaries.py", "test_kernel_import_guard.py",
    "test_manifests.py", "test_personalisation_guard.py", "test_registry_isolation.py",
    "test_tool_snapshots.py", "test_sync_contract.py",
})
# Applied as a plain `integrations_tree` marker rather than xdist's own
# `xdist_group`: measured locally, the group marker still spread one file's
# tests across four workers, so CI runs `-m "not integrations_tree" -n auto`
# and then `-m integrations_tree` serially — two steps, no plugin subtlety.


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.path.name in INTEGRATIONS_TREE_FILES:
            item.add_marker(pytest.mark.integrations_tree)
        if "db" not in item.keywords and DB_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.db)
        if "db" not in item.keywords:
            item.add_marker(pytest.mark.unit)


@pytest.fixture(scope="session")
def pg_url():
    """Connection URL for a disposable test Postgres with pgvector.

    Precedence: COMAR_TEST_DATABASE_URL (CI service container) →
    testcontainers (local Docker). Skips the db tier with a loud reason
    if neither is available — never silently passes.
    """
    env_url = os.environ.get("LIOS_TEST_DATABASE_URL") or os.environ.get("COMAR_TEST_DATABASE_URL")
    if env_url:
        yield env_url
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip(
            "db tier needs testcontainers (pip install 'testcontainers[postgres]') "
            "or COMAR_TEST_DATABASE_URL"
        )

    try:
        container = PostgresContainer("pgvector/pgvector:pg16", driver="psycopg2")
        container.start()
    except Exception as e:  # Docker not running / image pull failure
        pytest.skip(f"db tier needs Docker for testcontainers: {e}")

    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
def test_db(pg_url):
    """A coglib Database on the test Postgres, schema at head, users seeded.

    Bootstraps via the production fresh-DB path (`app.db._run_migrations`:
    create_tables + alembic stamp head). The full migration chain can NOT
    build a DB from empty — the baseline revision is a stamp-only no-op —
    so chain coverage comes from the downgrade/upgrade round-trip test in
    test_migrations.py instead.

    Users are seeded here because the alex/sam seed lives in the
    2026_05_02 migration, which the fresh-DB path skips.
    """
    from sqlalchemy import text

    from coglib import Database

    import app.models  # noqa: F401 — register every model on Base.metadata
    from app.db import _run_migrations

    # alembic/env.py overrides its URL from HOME_DATABASE__URL whenever set,
    # regardless of what the caller configured. Pin it to the test container
    # for the whole session so no alembic operation in tests can ever land on
    # a real database via a stray env var in the dev shell.
    saved = os.environ.get("HOME_DATABASE__URL")
    os.environ["HOME_DATABASE__URL"] = pg_url

    db = Database(url=pg_url)
    with db.session() as session:
        session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    _run_migrations(db)
    _seed_users(db)

    # Pin app.db's global singleton at session scope, not per-test. Several
    # things call app.db.get_db() from a background thread with no fixture
    # of its own — the AI usage ledger's daemon flush thread
    # (app/services/ai_ledger.py) is the one that surfaced this: it wakes on
    # its own 2s timer for the life of the process, independent of any
    # test's lifecycle. The old per-test `monkeypatch.setattr(app_db, "_db",
    # test_db)` reverted to `None` at every test's teardown, leaving a real
    # (if narrow) gap between tests during which a background `get_db()`
    # call would see `_db is None` and lazily create a second, *real*
    # `Database()` from production settings — which then stuck permanently
    # (`get_db()` only creates when `_db is None`), breaking every
    # subsequent test that relied on `get_db()` reaching the test container.
    # Rare at the original ~200ms/test pace; measurably more frequent once
    # `real_db`'s SAVEPOINT isolation removed that pace, since faster tests
    # mean more test-boundary gaps per wall-clock second. Setting it once
    # here and never reverting it mid-session closes the gap entirely.
    import app.db as app_db

    app_db._db = db

    # Suppress the AI usage ledger's background flush thread for the whole
    # db-tier session, not just per-`real_db`-test. It wakes on its own 2s
    # timer independent of any test, calling get_db().session() — which,
    # combined with `app_db._db` now pinned above for the whole session,
    # means it can reach the test container from ANY test (real_db's
    # SAVEPOINT-shared connection, real_db_concurrent's plain one, or the
    # gap between tests) and run a real, concurrent query/transaction
    # against it. That is a second live connection racing whatever the
    # current test is doing — measured to cause a genuine Postgres deadlock
    # against `real_db`'s long-lived transaction (AccessExclusiveLock vs
    # AccessShareLock across two backend pids) once the ledger thread had
    # already been started by an earlier test. `enqueue()` (an in-memory
    # ring buffer) stays live and harmless; only the thread that would flush
    # it to the database is suppressed — dropped rows here are an accepted,
    # designed-for outcome of the ledger (see its module docstring), not a
    # correctness gap. No restoration needed: this is scoped to the whole
    # db-tier test session, which is ending anyway when `test_db` tears down.
    from app.services import ai_ledger

    ai_ledger._reset_for_tests()
    # Patched on the *class*, not the `_LEDGER` instance: a handful of test
    # files (test_ai_ledger.py, test_algo_llm.py) call `_reset_for_tests()`
    # themselves mid-session, which builds a brand-new `_Ledger()` — an
    # instance-level patch here would be silently undone the moment any of
    # those ran, re-enabling the real thread for every test after it. Found
    # via a random-order run: `ai_usage_pkey` id=1 collided because the
    # thread had come back and written a real, non-rolled-back row.
    ai_ledger._Ledger._ensure_thread = lambda self: None

    yield db
    app_db._db = None
    db.engine.dispose()
    if saved is None:
        os.environ.pop("HOME_DATABASE__URL", None)
    else:
        os.environ["HOME_DATABASE__URL"] = saved


def _seed_users(db) -> None:
    from sqlalchemy import text

    with db.session() as session:
        session.execute(text(
            "INSERT INTO users (id, name, display_name, is_active, is_admin) "
            "VALUES (1, 'alex', 'Alex', true, true), (2, 'sam', 'Sam', true, false) "
            "ON CONFLICT (id) DO NOTHING"
        ))
        session.execute(text("SELECT setval('users_id_seq', 100, true)"))


def _reset_sequences(db) -> None:
    """Restart every sequence except `users_id_seq` back to 1.

    `real_db`'s SAVEPOINT rollback undoes every row a test wrote, but a
    Postgres sequence's `nextval` is deliberately **non-transactional** — it
    survives ROLLBACK by design, so concurrent transactions never block on
    sequence contention. Left alone, autoincrement ids climb across the
    whole suite instead of resetting per test, and several golden-snapshot
    tests (`test_tool_snapshots.py`) assert literal ids — a real, measured
    regression the first version of this fixture shipped with (id 1/2
    expected, id 3/4 seen, purely from running after other tests that had
    already advanced the same sequence).

    One `DO` block resets every sequence in a single round trip
    (~6ms measured, vs TRUNCATE's ~190ms) rather than one `ALTER SEQUENCE`
    per table. `ALTER SEQUENCE ... RESTART` *is* transactional (unlike
    `nextval`), so this runs on its own connection that commits immediately
    — inside `real_db`'s per-test transaction it would itself be undone by
    that same test's rollback, silently doing nothing.
    """
    from sqlalchemy import text

    with db.engine.connect() as conn:
        conn.execute(text(
            """
            DO $$
            DECLARE r RECORD;
            BEGIN
                FOR r IN SELECT sequencename FROM pg_sequences
                         WHERE schemaname = 'public' AND sequencename <> 'users_id_seq'
                LOOP
                    EXECUTE format('ALTER SEQUENCE %I RESTART WITH 1', r.sequencename);
                END LOOP;
            END $$;
            """
        ))
        conn.commit()


def _truncate_all_except_users(db) -> None:
    """Reset every table between tests, preserving the seeded users.

    Truncation (not transaction rollback) because code under test opens its
    own sessions via get_db().session() and commits — a savepoint-bound
    fixture session can't isolate those writes.
    """
    from sqlalchemy import text

    from coglib import Base

    tables = [
        t.name for t in Base.metadata.sorted_tables if t.name != "users"
    ]
    with db.session() as session:
        session.execute(text(
            f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"
        ))
    _seed_users(db)


@pytest.fixture
def real_db(test_db, monkeypatch):
    """Point the app's get_db() singleton at the test Postgres for one test,
    isolated via a per-test SAVEPOINT rolled back at teardown — not a
    TRUNCATE of all 79 tables.

    Standard SQLAlchemy "join a session into an external transaction"
    pattern: one connection is checked out for the whole test, a real
    transaction is opened on it, and `test_db.SessionLocal` is rebound
    (via `monkeypatch`, so it's restored after the test) to a sessionmaker
    bound to *that* connection with `join_transaction_mode="create_savepoint"`.
    Every session created during the test — the `db_session` fixture's
    session, and any production code path that opens its own via
    `app.db.get_db().session()`/`.SessionLocal()` (MCP auth, tool dispatch,
    scheduler state writes) — lands on the same connection, so a `commit()`
    from anywhere only ends a SAVEPOINT (SQLAlchemy transparently reopens
    one); it never reaches the real transaction. Rolling back the outer
    transaction at teardown undoes everything in one statement: no TRUNCATE,
    no re-seeding users, and — unlike TRUNCATE — no lock contention with a
    neighbouring worker's own truncate under `-n auto`.

    Note `app.db.get_db()`'s singleton itself (`app_db._db`) is pinned once
    at `test_db`'s session scope, not here — see that fixture's docstring
    for why a per-test monkeypatch/revert of it was a real, measured bug.

    ⚠️ Not safe for genuine multi-connection concurrency — two real sessions
    racing on the same row from separate threads. Sharing one connection
    across threads is unsafe *and* would defeat the very race such a test is
    trying to prove. Those tests use `real_db_concurrent` instead, which
    keeps the original independent-connection + TRUNCATE isolation.

    ⚠️ Binding the sessionmaker to a `Connection` (not the `Engine`) breaks
    any code that calls `session.get_bind().connect()` expecting an Engine
    to hand it a second, genuinely independent connection —
    `app/services/embedding.py::process_queue`'s single-flight advisory
    lock does exactly that, deliberately, on its own dedicated connection.
    `connection.connect` is shimmed below to satisfy that call by handing
    out a real connection from the engine's pool — which is the *correct*
    fix, not a workaround: an advisory lock sharing this test's own
    SAVEPOINT connection would defeat the single-flight guard it exists to
    provide (or deadlock), so it must never share it. Found via a real
    failure, not by inspection — see the mutation-check note in the PR body.

    Note the AI usage ledger's background flush thread is suppressed once,
    for the whole db-tier session, in `test_db` — see that fixture's
    docstring for the deadlock this fixture's shared connection made it
    trigger.
    """
    from sqlalchemy.orm import sessionmaker

    connection = test_db.engine.connect()
    connection.connect = test_db.engine.connect  # see docstring
    outer_trans = connection.begin()
    test_session_local = sessionmaker(
        bind=connection,
        autocommit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )

    monkeypatch.setattr(test_db, "SessionLocal", test_session_local)

    try:
        yield test_db
    finally:
        outer_trans.rollback()
        connection.close()
        _reset_sequences(test_db)


@pytest.fixture
def real_db_concurrent(test_db):
    """Like `real_db`, but for the handful of tests that need genuine
    independent DB connections at once — two real sessions racing on the
    same row from separate threads (`test_reminder_write_channel.py`'s
    `TestConcurrentDrainSafety`). `real_db`'s shared-connection SAVEPOINT
    would serialise (or corrupt) that race rather than exercise it, so
    isolation here is the original TRUNCATE-after-test instead.

    `app.db.get_db()`'s singleton is already pinned at `test_db`'s session
    scope (see that fixture) — nothing to monkeypatch here.
    """
    yield test_db
    _truncate_all_except_users(test_db)


@pytest.fixture
def db_session(real_db):
    """A plain session on the test Postgres (isolation via `real_db`'s
    per-test SAVEPOINT — see its docstring)."""
    session = real_db.SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def db_session_concurrent(real_db_concurrent):
    """Like `db_session`, but isolated via `real_db_concurrent`'s
    independent-connection + TRUNCATE instead of `real_db`'s shared-connection
    SAVEPOINT.

    For a test whose single session is handed to *several* real tool
    handlers in a row (`test_user_scoping.py`'s full-registry sweep) — some
    of which internally open their own further sessions (`system`'s daily
    brief aggregates many sources, each in its own `get_db().session()`
    call). Nesting that many SAVEPOINTs on one shared connection, several
    layers deep, with per-source try/except recovery at each layer, hit real
    SQLAlchemy desync errors (`PendingRollbackError`, "nested transaction
    already deassociated from connection") that never occur when each
    session gets its own real connection from the pool, as `real_db`'s
    original TRUNCATE-based predecessor gave every session — measured, not
    theorised: `real_db` alone made exactly these three tests fail.
    """
    session = real_db_concurrent.SessionLocal()
    yield session
    session.rollback()
    session.close()
