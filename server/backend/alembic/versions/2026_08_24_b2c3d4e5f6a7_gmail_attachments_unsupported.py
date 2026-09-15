"""backfill gmail message_attachments from pending to unsupported

`attachments_scan` queued every Gmail attachment as `pending`, while
`attachments_ingest` unconditionally rejected any row with `source='gmail'`
("source 'gmail' not supported yet") — so those rows were pending by
construction and could never drain. Fixed at the code level in
`app/integrations/attachments/sources.py` (a single SUPPORTED_INGEST_SOURCES
set read by both scan and ingest); this migration backfills the rows that
already accumulated before the fix.

Never deletes: these are real records of real attachments, flipped to
'unsupported' with a reason so they survive to be flipped back to 'pending'
once a Gmail download path lands (a one-line change to the shared set).

Revision ID: b2c3d4e5f6a7
Revises: b3d9e5a1c7f2
Create Date: 2026-08-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "b3d9e5a1c7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept identical to app.integrations.attachments.sources.unsupported_source_reason("gmail")
# — a migration can't import app code, so the string is duplicated here
# deliberately, and the downgrade below matches on it exactly.
_REASON = "source 'gmail' not supported yet"


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE message_attachments "
            "SET parse_status = 'unsupported', skip_reason = :reason "
            "WHERE source = 'gmail' AND parse_status = 'pending'"
        ).bindparams(reason=_REASON)
    )


def downgrade() -> None:
    # Only reverts rows carrying exactly this migration's reason string, so a
    # row some other process has since moved on from (e.g. re-supported and
    # ingested) is left alone.
    op.execute(
        sa.text(
            "UPDATE message_attachments "
            "SET parse_status = 'pending', skip_reason = NULL "
            "WHERE source = 'gmail' AND parse_status = 'unsupported' AND skip_reason = :reason"
        ).bindparams(reason=_REASON)
    )
