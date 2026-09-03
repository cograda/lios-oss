"""One-shot CLI: ingest claude.ai data-export conversations.json files.

Usage (inside the app container):
    docker compose exec app python -m scripts.ingest_claude_export \\
        --files /doc_corpus/claude-export/batch-0000-metadata+conversations.zip:conversations.json ...

Simplest usage — point at a directory containing extracted conversations.json
files (one per export batch) and it picks them all up:
    docker compose exec app python -m scripts.ingest_claude_export \\
        --dir /doc_corpus/claude-export-2026-07-26 --tags claude-conversations
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db  # noqa: E402
from app.integrations.historical_corpus.ingest import ingest_claude_export  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dir", type=Path,
        help="Directory to search recursively for conversations.json files",
    )
    ap.add_argument(
        "--files", type=Path, nargs="+",
        help="Explicit list of conversations.json file paths",
    )
    ap.add_argument("--tags", nargs="+", default=["claude-conversations"])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    paths: list[Path] = []
    if args.dir:
        if not args.dir.exists():
            print(f"ERROR: --dir {args.dir} does not exist", file=sys.stderr)
            return 2
        paths.extend(sorted(args.dir.rglob("conversations*.json")))
    if args.files:
        paths.extend(args.files)

    if not paths:
        print("ERROR: no conversations.json files found (use --dir or --files)", file=sys.stderr)
        return 2

    print(f"Ingesting {len(paths)} file(s):", file=sys.stderr)
    for p in paths:
        print(f"  {p}", file=sys.stderr)

    db = get_db()
    with db.session() as session:
        stats = ingest_claude_export(
            session, paths,
            project_tags=args.tags,
            on_progress=lambda msg: print(msg, file=sys.stderr),
        )

    print("\nDone.")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
