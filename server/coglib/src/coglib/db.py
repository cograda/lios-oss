"""Database engine, Base model, and session context manager."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from coglib.config import CogSettings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all project models."""


class Database:
    """Manages a SQLAlchemy engine + session factory."""

    def __init__(
        self,
        url: str,
        echo: bool = False,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_recycle: int = 3600,
        pool_pre_ping: bool = True,
    ) -> None:
        self.engine = create_engine(
            url,
            echo=echo,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_recycle=pool_recycle,
            pool_pre_ping=pool_pre_ping,
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def create_tables(self) -> None:
        """Create all tables registered on Base."""
        Base.metadata.create_all(bind=self.engine)
        logger.info("Database tables created")

    def drop_tables(self) -> None:
        """Drop all tables registered on Base."""
        Base.metadata.drop_all(bind=self.engine)
        logger.warning("Database tables dropped")

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """Context manager: auto-commits on success, rolls back on error."""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


_db: Database | None = None


def get_database(settings: CogSettings | None = None) -> Database:
    """Get or create the global Database instance from settings."""
    global _db
    if _db is None:
        if settings is None:
            settings = CogSettings()
        cfg = settings.database
        _db = Database(url=cfg.url, echo=cfg.echo, pool_size=cfg.pool_size)
    return _db
