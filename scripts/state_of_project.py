#!/usr/bin/env python3
"""Generate core/STATE.md — measured counts, not prose.

Backlog item S5.2: "the CLAUDE.md tool count is a rendered field, not
prose." Every prose count in this repo's CLAUDE.md files has drifted
(recorded, repeatedly, in core/CLAUDE.md and core/server/CLAUDE.md
themselves) because a number typed by hand is never updated by the change
that makes it wrong. This script measures live state and renders it; `make
-C core check-state` diffs the render against the committed copy so drift
fails CI instead of sitting in prose for a week.

Usage:
    core/server/backend/.venv/bin/python core/scripts/state_of_project.py
    core/server/backend/.venv/bin/python core/scripts/state_of_project.py --check

Must be run with a working interpreter for `core/server/backend` (the venv
there, or an environment with `pip install ./libs/coglib` plus
`requirements.txt`/`requirements-dev.txt`, as CI does). The script inserts
`core/server/backend` onto `sys.path` itself, so it does not need to be run
from that directory.

⚠️ The MCP tool count is environment-dependent, not a fixed constant —
see Code/CLAUDE.md's "MCP tools: measure, never quote" for the long version.
`register_mcp_tools()` at real startup additionally gates on
`is_configured()`, so a machine with no credentials configured registers
zero tools for every integration that checks config, and `sheets`,
`transcription` and `vision` report zero tools *in this environment*
regardless, because they are pure facades / caller-driven (no tools of
their own) rather than unconfigured. This script calls `mcp_tools()`
directly (the same call `register_all()` makes, without the `is_configured()`
gate applied at real MCP registration), so its number is the *unconditional*
count for this checkout, not a live-server count — it lists every
integration that contributed zero so the reader can tell "genuinely no
tools" from "not configured here" without re-deriving it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent
BACKEND = CORE_ROOT / "server" / "backend"
STATE_MD = CORE_ROOT / "STATE.md"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=True
    )
    return result.stdout


def measure_git_sha() -> str:
    return _run(["git", "rev-parse", "HEAD"], cwd=CORE_ROOT).strip()


def measure_integration_packages() -> list[str]:
    """Directories under app/integrations/ with a manifest.py.

    This is the disk-verifiable, environment-independent figure — see
    Code/CLAUDE.md: "Integration *packages* (directories) are the stable,
    disk-verifiable figure; the tool count is not."
    """
    integrations_dir = BACKEND / "app" / "integrations"
    return sorted(
        p.name
        for p in integrations_dir.iterdir()
        if p.is_dir()
        and not p.name.startswith("_")
        and p.name != "__pycache__"
        and (p / "manifest.py").exists()
    )


def measure_tools() -> tuple[int, list[str], list[str]]:
    """Return (total tools, registered integration names, zero-tool names).

    Environment-dependent — see the module docstring. Calls the same path
    `register_all()`/`get_all()` uses; does not apply the `is_configured()`
    gate real MCP registration applies on top of this.
    """
    import app.integrations as I

    I.register_all()
    registry = I.get_all()
    total = 0
    zero = []
    for name, integration in sorted(registry.items()):
        n = len(integration.mcp_tools())
        total += n
        if n == 0:
            zero.append(name)
    return total, sorted(registry.keys()), zero


def measure_tables() -> int:
    import app.models  # noqa: F401  (populates Base.metadata as a side effect)
    from coglib import Base

    return len(Base.metadata.tables)


def measure_test_counts() -> dict[str, int]:
    """pytest --collect-only counts for the two marker tiers. Cheap (~1-2s each)."""
    counts = {}
    for label, marker in (("not_db", "not db"), ("db", "db")):
        out = _run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", marker],
            cwd=BACKEND,
        )
        # Last non-blank line looks like: "1388/2310 tests collected (922 deselected) in 1.12s"
        # or, with nothing deselected: "2310 tests collected in 1.12s".
        last_line = next(line for line in reversed(out.strip().splitlines()) if line.strip())
        counts[label] = int(last_line.split("/")[0].split()[0])
    return counts


def measure_alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    return ", ".join(heads) if heads else "(none)"


def measure_workflows() -> list[str]:
    workflows_dir = CORE_ROOT.parent / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []
    return sorted(
        p.name for p in workflows_dir.iterdir() if p.suffix in (".yml", ".yaml")
    )


def measure_apps() -> list[str]:
    apps_dir = CORE_ROOT.parent / "apps"
    if not apps_dir.is_dir():
        return []
    return sorted(
        p.name for p in apps_dir.iterdir() if p.is_dir() and (p / "Makefile").exists()
    )


def render(data: dict) -> str:
    packages = data["integration_packages"]
    tool_total = data["tool_total"]
    registered = data["registered_integrations"]
    zero_tools = data["zero_tool_integrations"]
    tables = data["tables"]
    tests = data["test_counts"]
    alembic_head = data["alembic_head"]
    workflows = data["workflows"]
    apps = data["apps"]
    _ = data.get("git_sha")  # still measured, no longer rendered

    lines = []
    lines.append("# core — state of project")
    lines.append("")
    lines.append(
        "GENERATED by `scripts/state_of_project.py` — do not edit; run "
        "`make -C core state-of-project`."
    )
    lines.append("")
    # No git SHA line: the file is committed AT the commit it describes, so a
    # SHA in the body made every commit a "drift" and the check red by
    # construction (first CI run of PR #89). `git log -1 -- core/STATE.md`
    # answers the same question for free.
    lines.append("| Metric | Count | Notes |")
    lines.append("|---|---|---|")
    lines.append(
        f"| Integration packages | {len(packages)} | dirs under `app/integrations/` with a `manifest.py`; disk-verifiable, environment-independent |"
    )
    lines.append(
        f"| MCP tools (this environment) | {tool_total} | see warning below — environment-dependent, this is a lower bound |"
    )
    lines.append(f"| Registered integrations | {len(registered)} | via `register_all()` / `get_all()` |")
    lines.append(f"| DB tables | {tables} | `len(Base.metadata.tables)` after `import app.models` |")
    lines.append(f"| Tests (unit tier, `-m \"not db\"`) | {tests['not_db']} | |")
    lines.append(f"| Tests (db tier, `-m db`) | {tests['db']} | |")
    lines.append(f"| Tests (total collected) | {tests['not_db'] + tests['db']} | |")
    lines.append(f"| Alembic head revision | `{alembic_head}` | |")
    lines.append(f"| CI workflow files | {len(workflows)} | repo-root `.github/workflows/*.yml` |")
    lines.append(f"| Apps present (with Makefile) | {len(apps)} | `apps/*/Makefile`: {', '.join(apps) if apps else '(none)'} |")
    lines.append("")
    lines.append(
        "⚠️ **The MCP tool count is environment-dependent, and a lower bound "
        "on this machine, not a fixed constant.** `sheets`, `transcription` "
        "and `vision` contributed 0 tools when this was measured — they are "
        "pure facades / caller-driven integrations, not (necessarily) "
        "unconfigured ones; a machine with no credentials configured would "
        "report fewer still, because real registration additionally gates "
        "on `is_configured()`, which this measurement does not apply. "
        "Zero-tool integrations in this run: "
        + (", ".join(f"`{n}`" for n in zero_tools) if zero_tools else "(none)")
        + "."
    )
    lines.append("")
    lines.append(f"Integration packages ({len(packages)}): " + ", ".join(f"`{p}`" for p in packages))
    lines.append("")
    lines.append(f"Registered integrations ({len(registered)}): " + ", ".join(f"`{p}`" for p in registered))
    lines.append("")
    return "\n".join(lines) + "\n"


def gather() -> dict:
    tool_total, registered, zero_tools = measure_tools()
    return {
        "git_sha": measure_git_sha(),
        "integration_packages": measure_integration_packages(),
        "tool_total": tool_total,
        "registered_integrations": registered,
        "zero_tool_integrations": zero_tools,
        "tables": measure_tables(),
        "test_counts": measure_test_counts(),
        "alembic_head": measure_alembic_head(),
        "workflows": measure_workflows(),
        "apps": measure_apps(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Render to a temp buffer and diff against the committed core/STATE.md; exit 1 on drift.",
    )
    args = parser.parse_args(argv)

    rendered = render(gather())

    if args.check:
        if not STATE_MD.exists():
            print(f"check-state: FAILED — {STATE_MD} does not exist. Run `make -C core state-of-project`.", file=sys.stderr)
            return 1
        committed = STATE_MD.read_text()
        if committed != rendered:
            print("check-state: FAILED — core/STATE.md is stale. Run `make -C core state-of-project` and commit the result.", file=sys.stderr)
            print("--- committed", file=sys.stderr)
            print("+++ measured", file=sys.stderr)
            import difflib

            diff = difflib.unified_diff(
                committed.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
            )
            sys.stderr.writelines(diff)
            return 1
        print("check-state: OK — core/STATE.md matches live measurements.")
        return 0

    STATE_MD.write_text(rendered)
    print(f"Wrote {STATE_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
