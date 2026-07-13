# coglib

Shared Python package providing base config, database, logging, and utilities for all COG projects.

## Tech Stack

- Python 3.11+, Hatchling build
- Pydantic v2 + pydantic-settings (config)
- SQLAlchemy 2.0 (database)
- PostgreSQL via psycopg2-binary

## Structure

```
src/coglib/
├── config.py   # CogSettings base class (subclass per project)
├── db.py       # Database class, Base model, session context manager
├── logging.py  # Structured logging setup
└── utils.py    # parse_date() with multi-format fallback
```

## Usage

```bash
pip install -e /path/to/coglib        # Editable install
pip install -e "/path/to/coglib[dev]" # With dev tools
```

## Development

```bash
pytest          # Run tests
ruff check src/ # Lint
```

## Key Patterns

- Projects subclass `CogSettings` and set `env_prefix` in `model_config`
- Projects define models inheriting from `coglib.db.Base`
- `Database.session()` is a context manager that auto-commits/rollbacks
- `get_database()` returns a cached global instance
