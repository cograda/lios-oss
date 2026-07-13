"""Harness smoke tests + migration-chain coverage (db tier).

The baseline alembic revision is an empty stamp (production predates
alembic), so `upgrade head` can't build a schema from an empty database.
What we CAN regression-test:

  1. The production fresh-DB bootstrap path (create_tables + stamp head)
     produces a schema that alembic considers current — and that agrees
     with the ORM (no model change without a migration sneaks through).
  2. The recent migration chain round-trips: downgrade across the newest
     revisions, then upgrade back to head, on a scratch database.
"""

import pytest
from sqlalchemy import inspect, text

pytestmark = pytest.mark.db


def _alembic_config(url: str):
    from pathlib import Path

    from alembic.config import Config

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_schema_is_stamped_at_head(test_db):
    from alembic.script import ScriptDirectory

    cfg = _alembic_config(str(test_db.engine.url))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    with test_db.session() as session:
        current = session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()

    assert current == head


def test_all_orm_tables_exist(test_db):
    from coglib import Base

    existing = set(inspect(test_db.engine).get_table_names())
    missing = {t.name for t in Base.metadata.sorted_tables} - existing
    assert not missing, f"ORM tables missing from migrated schema: {missing}"


def test_users_seeded(db_session):
    from app.models.users import User

    names = {u.id: u.name for u in db_session.query(User).all()}
    assert names == {1: "alex", 2: "sam"}


def test_hnsw_index_present(test_db):
    indexes = inspect(test_db.engine).get_indexes("embeddings")
    assert any(ix["name"] == "ix_embeddings_hnsw" for ix in indexes)


def test_orm_and_migrations_agree(test_db):
    """No drift between models and the stamped schema (alembic check)."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from coglib import Base

    with test_db.engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": False, "compare_server_default": False}
        )
        diffs = compare_metadata(ctx, Base.metadata)

    assert not diffs, f"ORM/schema drift detected: {diffs}"


def test_recent_migration_chain_roundtrip(pg_url, test_db, monkeypatch):
    """Downgrade across the newest revisions and upgrade back to head.

    Runs on a scratch database so a mid-chain failure can't poison the
    shared test schema. Target: the revision just before the Phase 0
    block (2026-06-10), so all five Phase 0 migrations get exercised in
    both directions.
    """
    from alembic import command

    from coglib import Database
    from app.db import _run_migrations

    scratch = "comar_migration_roundtrip"
    with test_db.engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
        conn.execute(text(f"CREATE DATABASE {scratch}"))

    scratch_url = pg_url.rsplit("/", 1)[0] + f"/{scratch}"
    # alembic/env.py prefers HOME_DATABASE__URL over the passed config —
    # point it at the scratch DB or the round-trip would run on the shared one.
    monkeypatch.setenv("HOME_DATABASE__URL", scratch_url)
    db = Database(url=scratch_url)
    try:
        with db.session() as session:
            session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _run_migrations(db)

        cfg = _alembic_config(scratch_url)
        command.downgrade(cfg, "b1c2d3e4f5a6")  # pre-Phase-0 (mcp_oauth)
        command.upgrade(cfg, "head")
    finally:
        db.engine.dispose()
        with test_db.engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
