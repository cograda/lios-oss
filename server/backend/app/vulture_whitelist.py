"""Vulture whitelist — Wave 5.10 (2026-09-05) CI hygiene.

Not real code, never imported or executed. Vulture treats any name
*referenced* here as "used", so this file is where a genuinely
dynamically-reached symbol goes when vulture's static analysis can't see
the caller — SQLAlchemy models read only via `session.query(...)` string
lookups, FastAPI route handlers vulture can't trace through decorator
registration in some shapes, MCP tool handlers registered by name in a
manifest, Alembic revision functions, pytest fixtures used only via name
injection, and `manifest.py` fields read reflectively by the kernel
(`app.plugin.validate`/`app.plugin.discovery`).

Empty as of the first run (2026-09-05, min-confidence 80): the sweep found
exactly one finding, a genuinely-dead `Body` import in `app/routes/install.py`,
deleted rather than whitelisted. Add an entry here ONLY when you've verified
the symbol is reached dynamically — write the one-line reason as a comment
above it. When in doubt, whitelist rather than delete (a false delete breaks
production silently; a false whitelist entry just costs one line of noise).

Usage: `vulture app app/vulture_whitelist.py --min-confidence 80` (see the
`.github/workflows/core-tests.yml` "Dead code (vulture)" step).
"""
