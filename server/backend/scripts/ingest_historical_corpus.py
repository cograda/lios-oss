"""One-shot CLI: ingest the historical renovation corpus.

Usage (inside the app container):
    docker compose exec app python -m scripts.ingest_historical_corpus \\
        --root /doc_corpus --tags renovation [--limit-per-type N]

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
from app.integrations.historical_corpus.ingest import ingest_root  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True, help="Path to doc_corpus root")
    ap.add_argument("--tags", nargs="+", default=["renovation"], help="project_tags applied to every doc")
    ap.add_argument("--limit-per-type", type=int, default=None)
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
