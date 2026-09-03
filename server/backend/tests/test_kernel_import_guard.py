r"""Kernel <-> integration import boundary — V4 chunk 4.3e (unit tier).

The CI-side counterpart to `tests/test_capability_boundaries.py` (which
guards integration-to-integration imports). This one guards the other
direction: **kernel** packages must never reach into a *specific*
integration's internals. The only things a kernel file may import from
`app.integrations` are:

  - `app.integrations` itself, bare (`from app.integrations import get_all`,
    `get`, `register_all`, `INTEGRATIONS`) — this is the registry/discovery
    facade every kernel module is expected to use (`app/integrations/__init__.py`).
    Not "a specific integration's internals" — there's no `<pkg>` after
    `integrations` at all.
  - `app.integrations.base` — the shared `BaseIntegration` ABC every
    integration implements. Kernel modules that walk the tree
    (`app.plugin.discovery`, `app.plugin.validate`, `app.plugin.bases`) need
    this to identify integration classes; it's the kernel contract, not one
    integration's private internals.
  - `app.integrations.<pkg>.facade` — the declared capability surface (see
    `app/plugin/capabilities.py`). This is the *only* way a kernel route or
    module may call into one specific integration's behavior.
  - Dynamic imports built from a runtime string
    (`importlib.import_module(f"app.integrations.{name}.manifest")` etc.) —
    these are how discovery/validation *are supposed to* reach every
    integration generically; they're not a static dependency on one named
    package, so they're not something this test's static-import scan can or
    should flag (the scan only matches literal `from app.integrations.X
    import ...` / `import app.integrations.X` source lines).

Everything else is a violation — a kernel file quietly depending on one
integration's private module — UNLESS it's in `ALLOWLIST` below, with a
comment explaining why. Start at zero; every entry here was forced by a
real, already-existing dependency this chunk either fixed (routing it
through a facade) or found genuinely justified.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
INTEGRATIONS_DIR = BACKEND_DIR / "app" / "integrations"

# Kernel packages/files this test sweeps — exactly the list named in the
# chunk brief: app/plugin/, app/mcp/, app/models/, app/scheduler.py,
# app/main.py, app/routes/, app/services/.
KERNEL_PATHS = [
    BACKEND_DIR / "app" / "plugin",
    BACKEND_DIR / "app" / "mcp",
    BACKEND_DIR / "app" / "models",
    BACKEND_DIR / "app" / "scheduler.py",
    BACKEND_DIR / "app" / "main.py",
    BACKEND_DIR / "app" / "routes",
    BACKEND_DIR / "app" / "services",
]

# Matches `from app.integrations.<pkg>...` or `import app.integrations.<pkg>`
# — a *static* reference to one specific integration package. Deliberately
# does NOT match a bare `from app.integrations import ...` (no dot after
# "integrations" at all — that's the registry facade, always fine) or a
# dynamic `importlib.import_module(f"...")` call (not a literal import
# statement at all).
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+app\.integrations\.([a-zA-Z_0-9]+)")

_ALWAYS_ALLOWED_PKG = "base"  # app.integrations.base — the shared ABC, not a package's internals


def _is_facade_import(line: str, pkg: str) -> bool:
    return bool(re.search(rf"app\.integrations\.{re.escape(pkg)}\.facade\b", line))


# (relative file path, 1-based line number) -> justification. Every entry
# here is a real, existing static import of a specific integration's
# internals from kernel code that is NOT a `.facade` import — checked by
# hand, one at a time, and either fixed (routed through a new/extended
# facade — see `app.integrations.google_mail.facade`,
# `app.integrations.lastfm.facade`, `app.integrations.system.facade`, all
# added in this same chunk to close the real violations that existed before
# this test was written) or allowlisted below because fixing it isn't safe
# to do right now.
ALLOWLIST: dict[tuple[str, int], str] = {
    (
        "app/services/embedding.py", 46,
    ): (
        "Deliberate compat re-export (V4 chunk 3.4): `Embedding`/`EmbeddingQueue` "
        "used to live in this module and were relocated to "
        "app.integrations.embedding.models when `embedding` became a real "
        "integration package with its own manifest. A wide range of existing "
        "call sites (migrations, other integrations' models.py FK references, "
        "test fixtures) still import them from app.services.embedding — this "
        "one import keeps that surface working without a mass rename. Not a "
        "new dependency introduced by this chunk; documented, not fixed. "
        "Phase 2 widened the same import to the per-space vector tables and "
        "VECTOR_MODELS: this module is the only writer of those tables, and "
        "the mapping has to be reachable from the pipeline that fans a batch "
        "out across spaces."
    ),
    (
        "app/services/embedding.py", 53,
    ): (
        "app.integrations.embedding.cleaning:clean/CLEANER_VERSION. Same "
        "pre-existing coupling as the line above, not a new one: `embedding` is "
        "not a data-source integration but the pipeline itself, and this module "
        "IS that pipeline's enqueue/worker — only the ORM classes and the "
        "cleaners were relocated into the package. Cleaning has to run at the "
        "single enqueue chokepoint (so no producer can forget it, and so it "
        "happens before the length cap rather than after); pushing it behind a "
        "facade would mean a facade call per queued chunk for a pure function."
    ),
    (
        "app/routes/integrations.py", 372,
    ): (
        "app.integrations.historical_corpus.ingest:ingest_root, used by the "
        "manual '/integrations/historical_corpus/ingest' admin route. "
        "historical_corpus is explicitly out of scope for V4 chunk 4.3e "
        "(Alex has uncommitted local edits in that package's files) — this "
        "chunk's brief says not to open, edit, or stage anything under it. "
        "Routing this through a facade would mean adding a facade.py to "
        "historical_corpus, which is exactly the kind of edit that's off "
        "limits here. Deferred to historical_corpus's own conversion session."
    ),
}


def _violations() -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for base in KERNEL_PATHS:
        py_files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for py_file in py_files:
            if "__pycache__" in py_file.parts:
                continue
            rel = str(py_file.relative_to(BACKEND_DIR))
            for lineno, line in enumerate(py_file.read_text().splitlines(), start=1):
                m = _IMPORT_RE.match(line)
                if not m:
                    continue
                pkg = m.group(1)
                if pkg == _ALWAYS_ALLOWED_PKG:
                    continue
                if _is_facade_import(line, pkg):
                    continue
                if (rel, lineno) in ALLOWLIST:
                    continue
                violations.append((rel, lineno, line.strip()))
    return violations


def test_no_raw_kernel_to_integration_imports():
    violations = _violations()
    assert violations == [], (
        "Kernel code importing a specific integration's internals directly "
        "(must go through <pkg>.facade, or be added to ALLOWLIST with a "
        "justification):\n"
        + "\n".join(f"{f}:{n}: {line}" for f, n, line in violations)
    )


def test_allowlist_entries_still_exist_and_are_still_needed():
    """Guards against a stale allowlist: every entry must still point at a
    real line that would otherwise violate the rule (line numbers drift if
    the file is edited above the entry — this catches that, rather than
    silently allowlisting the wrong line forever)."""
    for (rel_path, lineno), reason in ALLOWLIST.items():
        assert reason.strip(), f"{rel_path}:{lineno} allowlist entry has no justification"
        py_file = BACKEND_DIR / rel_path
        assert py_file.exists(), f"allowlisted file {rel_path} no longer exists"
        lines = py_file.read_text().splitlines()
        assert 1 <= lineno <= len(lines), f"{rel_path}:{lineno} out of range — allowlist is stale"
        line = lines[lineno - 1]
        m = _IMPORT_RE.match(line)
        assert m, f"{rel_path}:{lineno} no longer matches an app.integrations.<pkg> import — stale entry, remove it"
