"""One-shot CLI: ingest the historical Riverside corpus.

Usage (inside the app container):
    docker compose exec app python -m scripts.ingest_historical_corpus \\
        --root /doc_corpus --tags riverside [--limit-per-type N]

Equipment manuals (different tree, different parser):
    docker compose exec app python -m scripts.ingest_historical_corpus \\
        --manuals --root /doc_corpus/Manuals

Or locally against a dev DB:
    cd server/backend && python -m scripts.ingest_historical_corpus --root /path/to/doc_corpus

The corpus is expected to be mounted into the container (add a volume to
docker-compose.yml pointing to the host `doc_corpus/` folder) so paths are
stable across re-runs and we can store them as `source_path` without leaking
local machine state.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make `app.*` importable when run as a script from server/backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db  # noqa: E402
from app.integrations.historical_corpus.ingest import (  # noqa: E402
    ingest_manuals, ingest_root,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True, help="Path to doc_corpus root")
    ap.add_argument("--tags", nargs="+", default=["riverside"], help="project_tags applied to every doc")
    ap.add_argument("--limit-per-type", type=int, default=None)
    # Equipment manuals are a different tree with a different parser, so they get an
    # explicit mode rather than being swept up by --root. Pointing the default mode at the
    # manuals tree would find no handler for their `.md` files at all — only `Voice Memos/`
    # is walked — so the failure would be a silent no-op rather than an error.
    ap.add_argument(
        "--manuals", action="store_true",
        help="Ingest a tree of normalised equipment manuals (markdown carrying `manual_of` "
             "frontmatter, produced by Documents/Reference/Manuals/_tools/build_text.py)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.root.exists():
        print(f"ERROR: --root {args.root} does not exist", file=sys.stderr)
        return 2

    db = get_db()
    with db.session() as session:
        if args.manuals:
            tags = args.tags if args.tags != ["riverside"] else ["manuals"]
            stats = ingest_manuals(
                session, args.root,
                project_tags=tags,
                on_progress=lambda msg: print(msg, file=sys.stderr),
            )
        else:
            stats = ingest_root(
                session, args.root,
                project_tags=args.tags,
                limit_per_type=args.limit_per_type,
                on_progress=lambda msg: print(msg, file=sys.stderr),
            )

    print("\nDone.")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
