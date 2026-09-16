#!/usr/bin/env python3
"""Vault integrity guard — surfaces silent sync damage.

Tracked mirror of `vault/.tools/vault_guard.py`. The vault is gitignored (see
`lios/CLAUDE.md`'s "What else lives here that git does not protect"), so the
script that `/kickoff`'s Track A and `/checkin` (`/refresh` until 2026-09-10)
now shell out to had no
version control at all — a single working copy on one Mac. This copy exists
so the tool has a git history and a disaster-recovery source; it is not
imported by anything (the commands run the vault copy directly, at
`vault/.tools/vault_guard.py`, since that's where the vault actually lives on
disk). Keep the two in sync by hand when either changes — there is no
mechanism enforcing that yet. The vault's own copy is authoritative for what
actually runs; this file must never be edited without also updating that one
(and per repo convention, this session does not edit files under `vault/`).

Two failure modes this catches, both of which have already bitten:

  1. Syncthing .sync-conflict-* files. Syncthing creates these when two
     writers diverge; it is a *rescue*, not an error. But nothing ever looked,
     so `Projects/Comar/Plans/Shipped/split-vault-and-dev-projects.md` sat at
     0 bytes from 2026-08-15 to 2026-08-20 while its only surviving copy sat
     beside it as a conflict file.

  2. Zero-byte markdown. A 0-byte write is not normal editor behaviour and has
     now happened at least four times. If a conflict twin exists it is almost
     certainly the real content.

Exit 0 = clean, 1 = something needs a human. Read-only; it never edits.
"""
import os, sys

SKIP = {".git", ".obsidian", ".embeddings", "node_modules", ".stversions"}

def walk(root="."):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for f in filenames:
            yield os.path.join(dirpath, f)

def main():
    conflicts, empties = [], []
    for p in walk():
        base = os.path.basename(p)
        if "sync-conflict" in base:
            conflicts.append(p)
        elif base.endswith(".md") and os.path.getsize(p) == 0:
            empties.append(p)

    if not conflicts and not empties:
        print("✓ vault guard: no sync conflicts, no zero-byte notes")
        return 0

    print("⚠️  VAULT GUARD — needs attention\n")

    if conflicts:
        print(f"{len(conflicts)} sync-conflict file(s) — a divergence nobody has read:")
        for c in conflicts:
            # the primary is the same path with the .sync-conflict-… suffix stripped
            base = os.path.basename(c)
            stem = base.split(".sync-conflict-")[0]
            primary = os.path.join(os.path.dirname(c), stem + ".md")
            csize = os.path.getsize(c)
            if os.path.exists(primary):
                psize = os.path.getsize(primary)
                verdict = ("PRIMARY IS TRUNCATED — restore from the conflict copy"
                           if psize == 0 else
                           "conflict copy is larger — check which is current"
                           if csize > psize else "primary looks intact — likely safe to delete the conflict")
                print(f"  • {primary}")
                print(f"      primary {psize:,}B vs conflict {csize:,}B  → {verdict}")
            else:
                print(f"  • {c} ({csize:,}B) — no primary found; this may be the only copy")
        print()

    if empties:
        print(f"{len(empties)} zero-byte note(s):")
        for e in empties:
            print(f"  • {e}")
        print("\n  Check for a .sync-conflict twin or .stversions/ copy before assuming loss.")
        print("  .stversions only keeps some files, so a conflict twin may be the ONLY copy.")

    return 1

if __name__ == "__main__":
    sys.exit(main())
