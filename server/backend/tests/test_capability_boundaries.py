r"""Cross-integration import boundary — V4 chunk 4.2 (unit tier).

The structural teeth of chunk 4.2: no integration package may import another
integration package's internals directly anymore. The only things one
package is allowed to import from another are:

  - its own package (self-imports are fine, obviously)
  - `app.integrations.base` (the shared `BaseIntegration` ABC — every
    integration's `__init__.py` imports this; it isn't "another package's
    internals", it's the kernel contract every integration implements)
  - `app.integrations.<other>.facade` (the declared capability surface —
    see `app/plugin/capabilities.py`)

Everything else — `app.integrations.<other>.models`, `.tools`, `.client`,
`.services`, `.writer`, `.ingest`, etc. reached from a *different* package —
is what this chunk eliminated. This test greps the same way a human would
(`git grep -n "from app.integrations\." app/integrations/`) but in pure
Python so it runs in the unit tier with no shell dependency, and asserts the
violator list is empty.
"""

from __future__ import annotations

import re
from pathlib import Path

INTEGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "integrations"

# Matches `from app.integrations.<pkg>` or `import app.integrations.<pkg>`
# (both forms appear in the tree — e.g. `from app.integrations.homeassistant
# import client as _client`).
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+app\.integrations\.([a-zA-Z_0-9]+)")

_ALLOWED_SUFFIXES = ("base",)  # app.integrations.base — the shared ABC, not a package's internals


def _own_package(py_file: Path) -> str:
    """The integration package a given .py file belongs to (its immediate
    parent dir under app/integrations/)."""
    rel = py_file.relative_to(INTEGRATIONS_DIR)
    return rel.parts[0]


def _is_facade_import(line: str, imported_pkg: str) -> bool:
    """True if the import line targets `app.integrations.<imported_pkg>.facade`
    (module or a name imported from it), the one allowed cross-package surface."""
    return bool(re.search(rf"app\.integrations\.{re.escape(imported_pkg)}\.facade\b", line))


def _violations() -> list[str]:
    violations: list[str] = []
    for py_file in sorted(INTEGRATIONS_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        own = _own_package(py_file)
        for lineno, line in enumerate(py_file.read_text().splitlines(), start=1):
            m = _IMPORT_RE.match(line)
            if not m:
                continue
            imported_pkg = m.group(1)
            if imported_pkg == own:
                continue  # self-import — fine
            if imported_pkg in _ALLOWED_SUFFIXES:
                continue  # app.integrations.base — the shared ABC
            if _is_facade_import(line, imported_pkg):
                continue  # declared capability surface — fine
            violations.append(f"{py_file.relative_to(INTEGRATIONS_DIR.parent.parent)}:{lineno}: {line.strip()}")
    return violations


def test_no_raw_cross_integration_imports():
    violations = _violations()
    assert violations == [], (
        "Raw cross-integration imports found (must go through a "
        "<pkg>.facade module instead):\n" + "\n".join(violations)
    )
