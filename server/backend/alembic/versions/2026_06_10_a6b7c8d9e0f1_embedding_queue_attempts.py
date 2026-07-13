"""embedding_queue_attempts: retry accounting for poison-item isolation

One failed subprocess batch used to mark all ~100 queue items 'error'
with no retry. The worker now bisects failed batches and retries items
up to MAX_EMBED_ATTEMPTS; this column carries the per-item count.

Also resets previously errored items to pending with a clean slate —
most were innocent cohort members of a single bad batch.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "embedding_queue",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    # Give errored items another chance under the new retry/bisect regime.
    op.execute(sa.text(
        "UPDATE embedding_queue SET status = 'pending', attempts = 0 "
        "WHERE status = 'error'"
    ))


def downgrade() -> None:
    op.drop_column("embedding_queue", "attempts")
