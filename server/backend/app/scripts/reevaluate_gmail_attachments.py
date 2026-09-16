"""One-off: re-evaluate Gmail attachment rows queued `unsupported` for their source.

Until 2026-09-07 `attachments_ingest` had no Gmail download path, so
`attachments_scan` recorded every Gmail attachment as `unsupported` with
"source 'gmail' not supported yet" (1,189 rows in production). Ingest can
now fetch them, so each of those rows should carry the verdict a fresh scan
would give it: `pending` when the mimetype has a parser, otherwise
`unsupported` with the *mimetype* reason. The logic is
`app.integrations.attachments.scan.reevaluate_unsupported_gmail_rows` —
this module is only its CLI.

Idempotent: the selection is exactly the rows still carrying the old source
reason, and every row touched loses it, so a second run reports 0 selected.
Never deletes, never downloads, never touches rows in any other state.

    # See what would change, write nothing (the default — no flag needed):
    python -m app.scripts.reevaluate_gmail_attachments

    # Actually write:
    python -m app.scripts.reevaluate_gmail_attachments --yes

Post-deploy, on the server (same shape as the install-code runbook):

    docker exec -it lios-core python -m app.scripts.reevaluate_gmail_attachments --yes
"""

from __future__ import annotations

import argparse
import json
import logging

from app.db import get_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually write (default: dry-run)")
    args = ap.parse_args()

    from app.integrations.attachments.scan import reevaluate_unsupported_gmail_rows

    dry_run = not args.yes
    with get_db().session() as session:
        stats = reevaluate_unsupported_gmail_rows(session, dry_run=dry_run)
    print(json.dumps(stats, indent=2))
    if dry_run:
        print("\ndry run — pass --yes to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
