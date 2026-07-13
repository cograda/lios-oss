"""Alembic migration environment.

Reads the database URL from HOME_DATABASE__URL (same env var as the app).
Imports all models so coglib.Base.metadata has the full schema for autogenerate.
"""

import logging
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Ensure the backend package is importable (alembic runs from backend/)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Import all models so they register on Base.metadata.
# This is the same import that app/models/__init__.py does.
import app.models  # noqa: F401

from coglib import Base

config = context.config

# Override sqlalchemy.url from environment if available.
# In Docker: HOME_DATABASE__URL is set by docker-compose.
# Locally: set it or pass via -x sqlalchemy.url=...
db_url = os.environ.get("HOME_DATABASE__URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

# Only apply alembic.ini's logging config when running standalone (alembic
# CLI). Migrations also run in-process at app startup (db.py); applying it
# there disabled the app's loggers and pinned root to WARNING, silencing all
# post-migration app logs for the life of the container.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL without a live connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connects to the database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
