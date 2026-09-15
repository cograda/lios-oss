"""historical_documents.raw_content_hash — dedup ingestion by raw bytes

Revision ID: f1c8a4d6e9b2
Revises: 846af90574fa
Create Date: 2026-09-07

Fixes #148. `content_hash` already exists but hashes the *parsed chunk
text* for one `source_path` — it lets a re-run of the same file skip
re-embedding, but does nothing when the same bytes arrive a second time
under a different filename or path (a re-saved scan, a renamed export).
This column is SHA256 of the raw source bytes, checked by
`ingest.py::_find_duplicate` before parsing.

Backfill: for every existing row whose `source_path` looks like a real
file (no `#`, which only email-thread and claude-export synthetic paths
carry — those have no single raw file to hash) we try to resolve it
against the doc_corpus mount(s) it could have been ingested from and hash
what we find. Most rows will still end up NULL: the container mounts
`/doc_corpus` read-only and only while a one-shot ingest is running,
so it is normally absent entirely, and the manuals tree is a different
root not derivable from `source_path` alone. NULL is a valid, expected
resting state here — the docstring on the column says so, and dedup
just never matches a NULL against a NULL raw hash the way it never
matches across owners (see `_find_duplicate`'s WHERE clause).
"""
import hashlib
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1c8a4d6e9b2"
down_revision: Union[str, Sequence[str], None] = "846af90574fa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Candidate roots a `source_path` might resolve under, in order. Only ever
# present on the deploy host during a one-shot ingest run; absent in dev
# and in CI, in which case every row is left NULL rather than erroring.
_CANDIDATE_ROOTS = (Path("/doc_corpus"), Path("/doc_corpus/Manuals"))


def _backfill_raw_hashes(conn) -> None:
    rows = conn.execute(
        sa.text(
            "SELECT id, source_path FROM historical_documents "
            "WHERE raw_content_hash IS NULL"
        )
    ).fetchall()
    for row in rows:
        # Snapshot the row's attrs up front — nothing here depends on an
        # ORM instance surviving past a commit, but the id/source_path are
        # read once and never re-touched after being used below.
        doc_id, source_path = row.id, row.source_path
        if not source_path or "#" in source_path:
            # Synthetic source_path (email_json thread, claude_export
            # conversation) — no single raw file backs it.
            continue
        data = None
        for root in _CANDIDATE_ROOTS:
            candidate = root / source_path
            try:
                if candidate.is_file():
                    data = candidate.read_bytes()
                    break
            except OSError:
                continue
        if data is None:
            continue
        digest = hashlib.sha256(data).hexdigest()
        conn.execute(
            sa.text(
                "UPDATE historical_documents SET raw_content_hash = :h WHERE id = :i"
            ),
            {"h": digest, "i": doc_id},
        )


def upgrade() -> None:
    op.execute(
        "ALTER TABLE historical_documents "
        "ADD COLUMN IF NOT EXISTS raw_content_hash VARCHAR(64)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_historical_documents_raw_content_hash "
        "ON historical_documents (raw_content_hash)"
    )
    _backfill_raw_hashes(op.get_bind())


def downgrade() -> None:
    # IF EXISTS throughout: a downgrade re-run after a half-applied step
    # must not fail on the very objects it is there to remove.
    op.execute("DROP INDEX IF EXISTS ix_historical_documents_raw_content_hash")
    op.execute(
        "ALTER TABLE historical_documents DROP COLUMN IF EXISTS raw_content_hash"
    )
