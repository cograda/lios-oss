#!/usr/bin/env python3
"""Deliver Alex's day-to-day slash-command set into the vault project.

Since the dev/day-to-day project split (2026-07-28), Alex's day-to-day
commands live in `vault/.claude/commands/` — the vault is its own Claude Code
project root, and the repo root is the *development* project (which needs no
day-to-day commands at all; `/sync-docs` is user-level in
`~/.claude/commands/`).

`vault/` is gitignored, so the delivered files are a BUILD ARTIFACT. The
versioned sources are:

  * 6 generated commands — `server/backend/app/prompts/templates/*.md.j2`,
    rendered per-user by `app.prompts.commands` (sam-rollout Phase B2).
    These carry a `<!-- GENERATED -->` header. Never hand-edit the delivered
    file; edit the template and re-run this script.
  * 16 static commands — `commands/*.md` at the repo root. Hand-authored,
    Alex-specific, copied through verbatim. Edit these directly.

Sam's equivalent set is delivered by the installer from
`GET /api/v1/commands` into her `~/Comar/` working folder. That path is
untouched by this script — the two users' delivery mechanisms are separate on
purpose, because she never has the repo.

Usage:
    server/backend/.venv/bin/python scripts/render_commands.py [--check]

--check exits 1 (and prints a diff) if delivering would change any file,
without writing anything.

CI does not run --check against the delivered files (they're gitignored and
absent on a fresh clone). The equivalent CI guard is the golden-snapshot
comparison in `tests/test_command_registry.py::TestNoDriftFromRegistry`,
which asserts the rendered six match `tests/snapshots/commands/*.md`.
Regenerate those deliberately with UPDATE_COMMAND_SNAPSHOTS=1 pytest.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "server" / "backend"))

# The vault moved from `comar/vault/` to `lios/vault/` on 2026-09-01 — one
# level above this element's root. Found broken 2026-09-02, the first time a
# command was added after the move: the eleventh fixed-path casualty of the
# migration. Prefer the monorepo location; fall back to the old one so a
# standalone checkout still works.
def _vault_root() -> Path:
    for candidate in (REPO_ROOT.parent / "vault", REPO_ROOT / "vault"):
        if candidate.is_dir():
            return candidate
    return REPO_ROOT.parent / "vault"

from app.prompts.commands import render_command_set  # noqa: E402

# Delivered artifact — the vault is Alex's day-to-day Claude Code project.
TARGET_DIR = _vault_root() / ".claude" / "commands"

# Versioned source for the hand-authored (non-generated) commands.
STATIC_DIR = REPO_ROOT / "commands"

# Files in STATIC_DIR that document the directory rather than being commands.
STATIC_NON_COMMANDS = {"README.md"}


def static_command_paths() -> list[Path]:
    return [
        p for p in sorted(STATIC_DIR.glob("*.md"))
        if p.name not in STATIC_NON_COMMANDS
    ]


def collect() -> dict[str, str]:
    """Every file that should end up in TARGET_DIR, filename → content.

    Rendered with `prefs=None`, i.e. **every** conditional block included.
    `GET /api/v1/commands` renders against the caller's stored preferences and
    so drops the blocks they've switched off; this script can't, because
    preferences live in the server's `user_preferences` table and there is no
    per-caller endpoint to read them from on the bearer surface (only the
    UI-token-gated `/api/preferences/{user_id}`).

    That's a real divergence, deliberately left: it is correct only while Alex
    uses every section. If he ever switches one off, this delivers a file that
    disagrees with his own settings — the same class of silent drift that let
    Sam run a two-week-old command. The fix when that day comes is a
    preferences read on the `/api/v1` surface, not a local override file.
    """
    out: dict[str, str] = dict(render_command_set("alex"))

    for path in static_command_paths():
        if path.name in out:
            raise SystemExit(
                f"{path.name} exists both as a generated template and as a "
                f"static file in commands/ — remove one. The generated set is "
                f"the source of truth for the six curated commands."
            )
        out[path.name] = path.read_text(encoding="utf-8")

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Don't write files; exit non-zero if delivery would change anything.",
    )
    args = parser.parse_args()

    if not STATIC_DIR.is_dir():
        print(f"missing static command source: {STATIC_DIR}", file=sys.stderr)
        return 1

    if not TARGET_DIR.parent.parent.is_dir():
        print(
            f"vault not found at {TARGET_DIR.parent.parent} — this script "
            "delivers into the vault project and must run on a machine that "
            "has the vault checked out.",
            file=sys.stderr,
        )
        return 1

    expected = collect()
    if not args.check:
        TARGET_DIR.mkdir(parents=True, exist_ok=True)

    dirty = False

    for filename, content in sorted(expected.items()):
        path = TARGET_DIR / filename
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == content:
            print(f"  {filename}: unchanged")
            continue

        dirty = True
        if args.check:
            print(f"  {filename}: WOULD CHANGE")
            if existing is not None:
                diff = difflib.unified_diff(
                    existing.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{filename}", tofile=f"b/{filename}",
                )
                sys.stdout.writelines(diff)
        else:
            path.write_text(content, encoding="utf-8")
            print(f"  {filename}: written")

    # Delivered dir is generated, so anything not in `expected` is a leftover
    # (a renamed or retired command). Remove it rather than letting a stale
    # command linger in the day-to-day project.
    if TARGET_DIR.is_dir():
        for path in sorted(TARGET_DIR.glob("*.md")):
            if path.name in expected:
                continue
            dirty = True
            if args.check:
                print(f"  {path.name}: WOULD BE REMOVED (no longer a source)")
            else:
                path.unlink()
                print(f"  {path.name}: removed (no longer a source)")

    if args.check and dirty:
        print(
            f"\n{TARGET_DIR} is stale — run scripts/render_commands.py",
            file=sys.stderr,
        )
        return 1

    if not args.check:
        print(f"\n{len(expected)} commands delivered to {TARGET_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
