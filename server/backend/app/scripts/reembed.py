"""Re-clean the index, or fill a newly-added embedding space.

A script rather than an MCP tool or a route: both jobs walk ~58k rows and the
second one makes tens of thousands of API calls, which is well past any request
timeout. Run it deliberately, watch it, and let the ordinary embedding worker
drain the queue afterwards.

    # See what would change, write nothing:
    python -m app.scripts.reembed reclean --dry-run

    # Re-offer every chunk through the current cleaners:
    python -m app.scripts.reembed reclean --yes

    # After turning a provider on, vectorise existing chunks into its space:
    python -m app.scripts.reembed fill-space gemini-embedding-2 --yes

**Order matters.** `reclean` changes the text; `fill-space` vectorises the text
as it stands. Running fill-space first builds the new space from the old text
and then reclean invalidates it, paying for the same vectors twice.

`reclean` only *queues* work — the `*/5` embedding worker does the embedding, so
the queue drains over the following hours and search degrades for nothing in the
meantime (old vectors stay until their replacement lands).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.db import get_db
from app.integrations.embedding import backfill

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rc = sub.add_parser("reclean", help="re-offer every chunk through the cleaners")
    rc.add_argument("--sources", help="comma-separated subset, e.g. email,vault")
    rc.add_argument("--limit", type=int, help="stop after N rows (smoke test)")
    rc.add_argument("--no-boilerplate", action="store_true",
                    help="skip the corpus-wide BoilerplateFilter")

    fs = sub.add_parser("fill-space", help="vectorise existing chunks into one space")
    fs.add_argument("provider_id")
    fs.add_argument("--limit", type=int)
    fs.add_argument("--batch-size", type=int, default=64)

    dr = sub.add_parser("drain", help="process the embedding queue now, until empty")
    # 200, not 500: one batch must finish inside
    # EMBED_SUBPROCESS_TIMEOUT_SECONDS (300s) or it times out and bisects,
    # doing the work twice, and its peak memory is the batch's whole text at
    # once. 500 items of long chunks is what put the box at 197 MB free.
    dr.add_argument("--batch-size", type=int, default=200)

    pb = sub.add_parser("prune-blank",
                        help="delete embedding rows whose text cleans to nothing")

    for p in (rc, fs, dr, pb):
        p.add_argument("--yes", action="store_true", help="actually write")
        p.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()
    if not args.yes and not args.dry_run:
        print("refusing to run: pass --dry-run or --yes", file=sys.stderr)
        return 2
    dry = not args.yes

    with get_db().session() as session:
        if args.cmd == "reclean":
            sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
            stats = backfill.reclean(
                session,
                sources=sources,
                limit=args.limit,
                use_boilerplate=not args.no_boilerplate,
                dry_run=dry,
            )
            print(json.dumps(stats, indent=2))
            if not dry:
                print(f"\nqueued — the */5 worker will drain "
                      f"{backfill.pending_queue_depth(session):,} pending items")
        elif args.cmd == "fill-space":
            stats = backfill.fill_space(
                session,
                args.provider_id,
                batch_size=args.batch_size,
                limit=args.limit,
                dry_run=dry,
            )
            print(json.dumps(stats, indent=2))
        elif args.cmd == "prune-blank":
            n = backfill.prune_blank_chunks(session, dry_run=dry)
            print(f"{n:,} blank chunk(s) {'found' if dry else 'deleted'}")
        else:
            depth = backfill.pending_queue_depth(session)
            print(f"{depth:,} pending")
            if dry:
                return 0
            total = drain(session, args.batch_size, depth)
            print(f"\ndrained {total:,}; {backfill.pending_queue_depth(session):,} left")
    return 0


def drain(session, batch_size: int, expected: int) -> int:
    """Process the queue until empty, instead of waiting on the `*/5` cron.

    The scheduled worker does 100 items per 5 minutes — fine for steady state,
    but 48 hours for a 58k re-enqueue, during which the index is half old text
    and half new.

    Runs alongside the scheduler rather than instead of it, which is safe but
    slightly wasteful: `process_queue` reclaims any row still marked
    'processing' at entry, assuming it was abandoned by a killed run. That
    assumption held while the scheduler was the only caller. With two callers,
    a cron cycle can reclaim rows this drain has in flight and embed them
    concurrently. Both then delete-by-(source, source_id) before inserting, so
    the outcome is one correct row either way — the cost is duplicated CPU on
    at most ~100 items per 5-minute cycle, which against 58k is noise.
    """
    from app.services.embedding import EmbeddingService

    total = 0
    while True:
        n = EmbeddingService.process_queue(session, batch_size=batch_size)
        if not n:
            break
        total += n
        # None (rendered "n/a"), not 100, when `expected` is zero — a
        # re-enqueue of nothing is not "100% drained", it's "there was
        # nothing to measure". See the "honest numbers" hardening pass.
        pct = (total / expected * 100) if expected else None
        pct_str = f"{pct:.1f}%" if pct is not None else "n/a"
        logging.info("drained %s/%s (%s)", f"{total:,}", f"{expected:,}", pct_str)
    return total


if __name__ == "__main__":
    raise SystemExit(main())
