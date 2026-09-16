"""One-off backfill: R4 provenance (`source_date` / `is_history`) onto
existing `embeddings` rows.

Every row written by an integration's writer *since* R4 (2026-09-04) already
carries these keys in `metadata_json` (see `obsidian/sync.py::_chunk_metadata`
for the vault producer's shape). Rows written before that have neither key,
so `EmbeddingService.search()`'s read-time fallback (`source_date` defaults to
`created_at`, `is_history` defaults to `False`) is silently doing the work
this script makes explicit and idempotent-cheap to re-derive properly where
we *can* do better than that fallback — specifically vault rows, which carry
enough in their existing metadata (and can be re-derived from the file on
disk) to set a real `is_history` rather than the safe-but-uninformed default.

**Never re-embeds, never touches a vector.** This writes `metadata_json` only
— the same "free" operation `obsidian/sync.py::refresh_status_metadata`
already relies on: the per-space vector tables are keyed on `embedding_id` and
are not touched here.

Idempotent: a row that already has both `source_date` and `is_history` keys
is left alone, so re-running after new writers land costs nothing beyond a
metadata parse per row.

    # See what would change, write nothing (the default — no flag needed):
    python -m app.scripts.backfill_provenance

    # Actually write:
    python -m app.scripts.backfill_provenance --yes

    # Restrict to one source (mainly useful for vault, the one source this
    # script can do better than the generic fallback for):
    python -m app.scripts.backfill_provenance --source vault --yes
"""

from __future__ import annotations

import argparse
import json
import logging

from app.db import get_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _needs_backfill(meta: dict) -> bool:
    return "source_date" not in meta or "is_history" not in meta


def _vault_provenance(meta: dict, source_id: str) -> tuple[str | None, bool]:
    """Best-effort provenance for an already-indexed vault chunk.

    `modified_at` already rides the metadata for every vault row (predates
    R4) — reuse it as `source_date` rather than falling back to `created_at`
    (when the chunk was embedded, a strictly worse proxy for "when is this
    content from"). `is_history` is re-derived from the same rule the live
    writer uses (`obsidian.sync.is_history_chunk`), against the row's own
    `status` metadata and its path — the file on disk isn't needed for
    either input.
    """
    from app.integrations.obsidian.sync import is_history_chunk

    source_date = meta.get("modified_at")
    doc_path = source_id.split("#", 1)[0]
    is_history = is_history_chunk(doc_path, meta.get("status"))
    return source_date, is_history


def backfill_provenance(
    session, source: str | None = None, limit: int | None = None, dry_run: bool = True,
) -> dict:
    from app.services.embedding import Embedding

    q = session.query(Embedding.id, Embedding.source, Embedding.source_id, Embedding.metadata_json)
    if source:
        q = q.filter(Embedding.source == source)
    if limit:
        q = q.limit(limit)

    scanned = 0
    updated = 0
    by_source: dict[str, int] = {}

    for row_id, row_source, source_id, meta_json in q.all():
        scanned += 1
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}

        if not _needs_backfill(meta):
            continue

        if row_source == "vault":
            source_date, is_history = _vault_provenance(meta, source_id)
        else:
            # Generic fallback for every other source: no per-row content to
            # re-derive `is_history` from here (that would mean importing
            # each producer's own path/status conventions, which is exactly
            # the per-integration knowledge this backfill script — living in
            # the kernel-shared `embedding` surface, not any one integration
            # — shouldn't need). `source_date` stays unset so `search()`'s
            # own fallback (created_at) applies at read time; only
            # `is_history` is made explicit, since "unknown" and "not
            # history" are the same safe default.
            source_date = meta.get("source_date")
            is_history = bool(meta.get("is_history", False))

        meta.setdefault("is_history", is_history)
        if source_date is not None:
            meta.setdefault("source_date", source_date)

        by_source[row_source] = by_source.get(row_source, 0) + 1
        updated += 1

        if not dry_run:
            session.query(Embedding).filter(Embedding.id == row_id).update(
                {"metadata_json": json.dumps(meta)}, synchronize_session=False
            )

    if not dry_run and updated:
        session.commit()

    return {
        "scanned": scanned,
        "updated": updated,
        "by_source": by_source,
        "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", help="restrict to one embedding source, e.g. vault")
    ap.add_argument("--limit", type=int, help="stop after N rows (smoke test)")
    ap.add_argument("--yes", action="store_true", help="actually write (default: dry-run)")
    args = ap.parse_args()

    dry_run = not args.yes
    with get_db().session() as session:
        stats = backfill_provenance(session, source=args.source, limit=args.limit, dry_run=dry_run)
    print(json.dumps(stats, indent=2))
    if dry_run:
        print("\ndry run — pass --yes to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
