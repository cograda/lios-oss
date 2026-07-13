"""Shared test fixtures for server tests.

Two tiers, selected by marker:

  unit  (default) — no database, no external services; DB interactions
        mocked. Fast and infrastructure-free. Everything not explicitly
        marked `db` gets this marker automatically.
  db    — real Postgres (pgvector). Uses COMAR_TEST_DATABASE_URL if set
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

# Add vendored coglib (shared package — installed in Docker, but needs a
# path hint for local pytest runs)
_coglib_candidates = [
    Path(__file__).resolve().parent.parent.parent / "coglib" / "src",  # server/coglib
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
        "markers", "db: requires real Postgres (testcontainers or COMAR_TEST_DATABASE_URL)"
    )


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "db" not in item.keywords:
            item.add_marker(pytest.mark.unit)


@pytest.fixture(scope="session")
def pg_url():
    """Connection URL for a disposable test Postgres with pgvector.

    Precedence: COMAR_TEST_DATABASE_URL (CI service container) →
    testcontainers (local Docker). Skips the db tier with a loud reason
    if neither is available — never silently passes.
    """
    env_url = os.environ.get("COMAR_TEST_DATABASE_URL")
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
    yield db
    db.engine.dispose()
    if saved is None:
        os.environ.pop("HOME_DATABASE__URL", None)
    else:
        os.environ["HOME_DATABASE__URL"] = saved


def _seed_users(db) -> None:
    from sqlalchemy import text

    with db.session() as session:
        session.execute(text(
            "INSERT INTO users (id, name, display_name, is_active) "
            "VALUES (1, 'alex', 'Alex', true), (2, 'sam', 'Sam', true) "
            "ON CONFLICT (id) DO NOTHING"
        ))
        session.execute(text("SELECT setval('users_id_seq', 100, true)"))


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
    """Point the app's get_db() singleton at the test Postgres for one test.

    Any production code path that calls app.db.get_db() — MCP auth, tool
    dispatch, scheduler state writes — lands on the test container. Tables
    are truncated (users re-seeded) after the test.
    """
    import app.db as app_db

    monkeypatch.setattr(app_db, "_db", test_db)
    yield test_db
    _truncate_all_except_users(test_db)


@pytest.fixture
def db_session(real_db):
    """A plain session on the test Postgres (isolation via real_db truncate)."""
    session = real_db.SessionLocal()
    yield session
    session.rollback()
    session.close()
