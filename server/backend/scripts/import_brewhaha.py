"""One-shot importer: brewhaha CSV → comar coffees table.

Usage (run inside the app container, after deploy + migration):
    docker compose exec app python scripts/import_brewhaha.py /path/to/coffee_collection_brewhaha.csv

Or pass --dry-run to print without writing.

Idempotent — upserts by (name, roaster). Re-running updates existing rows.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Make `app.*` importable when run as a script from server/backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db
from app.integrations.coffee.models import Coffee
from app.integrations.coffee.tools import _enqueue_coffee_embedding

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("import_brewhaha")


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    return s or None


def _to_decimal(v: str | None) -> Decimal | None:
    s = _clean(v)
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _to_int(v: str | None) -> int | None:
    s = _clean(v)
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _row_hash(row: dict) -> str:
    keys = sorted(k for k in row if not k.startswith("_"))
    payload = "|".join(f"{k}={row.get(k) or ''}" for k in keys)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def import_csv(path: Path, dry_run: bool = False) -> dict:
    if not path.exists():
        raise FileNotFoundError(path)

    created = 0
    updated = 0
    skipped = 0

    db = get_db()
    with db.session() as session:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                name = _clean(row.get("name"))
                if not name:
                    skipped += 1
                    continue
                roaster = _clean(row.get("roaster"))

                row_hash = _row_hash(row)
                existing = (
                    session.query(Coffee)
                    .filter(Coffee.name == name, Coffee.roaster == roaster)
                    .first()
                )

                if existing and existing.content_hash == row_hash:
                    skipped += 1
                    continue

                target = existing or Coffee(name=name, roaster=roaster)
                target.origin_country = _clean(row.get("origin_country"))
                target.region_farm = _clean(row.get("region_farm"))
                target.process = _clean(row.get("process"))
                target.fermentation = _clean(row.get("fermentation"))
                target.variety = _clean(row.get("variety"))
                target.altitude_masl = _clean(row.get("altitude_masl"))
                target.category = _clean(row.get("category"))
                target.weight_g = _to_decimal(row.get("weight_g"))
                target.roaster_tasting_notes = _clean(row.get("roaster_tasting_notes"))
                target.notes = _clean(row.get("notes"))
                target.status = _clean(row.get("status")) or "current"
                target.rating = _to_int(row.get("rating"))
                target.source_id = f"brewhaha:{name}|{roaster or ''}"
                target.synced_at = datetime.now(timezone.utc)
                target.content_hash = row_hash
                target.updated_at = datetime.now(timezone.utc)

                if existing is None:
                    session.add(target)
                    created += 1
                    logger.info("CREATE %s — %s", roaster or "?", name)
                else:
                    updated += 1
                    logger.info("UPDATE %s — %s", roaster or "?", name)

                session.flush()  # need id for embedding enqueue
                _enqueue_coffee_embedding(session, target)

        if dry_run:
            session.rollback()
            logger.info("Dry run — no changes committed.")
        else:
            session.commit()

    summary = {"created": created, "updated": updated, "skipped": skipped}
    logger.info("Done. %s", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Import brewhaha CSV into comar coffees.")
    parser.add_argument("csv", type=Path, help="Path to coffee_collection_brewhaha.csv")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report, no DB writes.")
    args = parser.parse_args()
    import_csv(args.csv, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
