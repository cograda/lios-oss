"""Database setup using coglib + Alembic migrations."""

import logging
import os
from collections.abc import Generator
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from coglib import Database, get_database

from app.config import settings

logger = logging.getLogger(__name__)

_db: Database | None = None

# Alembic config lives at backend/alembic.ini
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _run_migrations(db: Database) -> None:
    """Run pending Alembic migrations.

    On a fresh database (no alembic_version table), falls back to
    create_all() + stamp so existing deployments aren't broken.
    """
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(_ALEMBIC_INI))
    # Point alembic at our engine's URL (already connected). Must render with
    # the real password — str(URL) masks it as '***', which alembic would then
    # use verbatim. (In Docker this was hidden by alembic/env.py overriding
    # the URL from HOME_DATABASE__URL; without that env var the masked URL
    # fails auth.)
    alembic_cfg.set_main_option(
        "sqlalchemy.url", db.engine.url.render_as_string(hide_password=False)
    )

    with db.session() as session:
        # Check if alembic_version table exists (i.e., migrations have been initialised)
        result = session.execute(text(
            "SELECT EXISTS ("
            "  SELECT FROM information_schema.tables "
            "  WHERE table_name = 'alembic_version'"
            ")"
        ))
        has_alembic = result.scalar()

    if has_alembic:
        # Normal path: run any pending migrations
        logger.info("Running Alembic migrations...")
        command.upgrade(alembic_cfg, "head")
    else:
        # First time with Alembic on this DB.
        # Check if tables already exist (existing deployment) or it's truly fresh.
        with db.session() as session:
            result = session.execute(text(
                "SELECT EXISTS ("
                "  SELECT FROM information_schema.tables "
                "  WHERE table_name = 'sync_state'"
                ")"
            ))
            has_existing_tables = result.scalar()

        if has_existing_tables:
            # Existing DB — stamp the first (baseline) revision, then upgrade
            # to run any subsequent migrations that add/alter columns.
            from alembic.script import ScriptDirectory
            script = ScriptDirectory.from_config(alembic_cfg)
            base_rev = script.get_revision("base")
            if base_rev is None:
                # No migrations at all — nothing to do
                logger.info("No Alembic migrations found, skipping")
                return
            # Walk to find the first concrete revision
            bases = list(script.get_bases())
            base_id = bases[0] if bases else "head"
            logger.info(f"Existing database detected — stamping baseline ({base_id}), then upgrading")
            command.stamp(alembic_cfg, base_id)
            command.upgrade(alembic_cfg, "head")
        else:
            # Truly fresh DB — create all tables, then stamp
            logger.info("Fresh database — creating tables and stamping baseline")
            db.create_tables()
            # create_tables() only knows ORM-mapped objects. A handful of
            # migrations create plain Postgres objects via raw op.execute()
            # that have no ORM representation at all — this fast path has
            # to recreate those by hand, or a from-scratch DB (every test's
            # fixture path) silently lacks them even though a real
            # `alembic upgrade head` run would have them. Currently just
            # snag_uid_seq (see 2026_07_07_d0e1f2a3b4c5_snags.py).
            with db.session() as session:
                session.execute(text("CREATE SEQUENCE IF NOT EXISTS snag_uid_seq START 1"))
                session.commit()
            command.stamp(alembic_cfg, "head")


def get_db() -> Database:
    global _db
    if _db is None:
        _db = get_database(settings)
        # Ensure pgvector extension exists before anything else
        with _db.session() as session:
            session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            session.commit()
        # Run Alembic migrations (or create_tables + stamp on first run)
        _run_migrations(_db)
    return _db


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session."""
    db = get_db()
    with db.session() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
