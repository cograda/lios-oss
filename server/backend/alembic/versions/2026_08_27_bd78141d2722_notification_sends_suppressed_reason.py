"""notification_sends.suppressed_reason — push-boundary gating (flap fix).

`vault/Projects/lios/Backlog.md`: "Push notifications flap all night" —
~20 pushes/day to one phone, dominated by `macbook:daemon_silent` and
`apple_reminders:data_stale`, each firing and self-resolving in 15-45
minutes, repeating every 30-60 minutes around the clock. Root cause: a
sleeping laptop (lid closed) is expected state, not an incident, and the
sweep had no notion of "wait and see" before ringing a phone.

Three push-boundary mechanisms land with this column (`sweep.py`):
persistence gate (an alert must be continuously active for
`min_active_minutes` before it may push at all), re-fire cooldown (a
fingerprint that just resolved will not push again for
`refire_cooldown_minutes`), and quiet hours (non-critical pushes are held
overnight and delivered once at window end if still active). Detection is
unchanged — the axes in `system/tools.py` keep firing exactly as before;
only whether a detected alert reaches a phone changes.

`suppressed_reason` is nullable and NULL for the overwhelming majority of
rows — anything that pushed normally, or an alert not yet even candidate for
a push. It exists so a held alert is *explainable*: without it, a fingerprint
sitting open with `send_count == 0` looks identical whether the sweep is
deliberately waiting out the persistence gate or has silently broken. Set to
one of "min_active_gate" | "refire_cooldown" | "quiet_hours" when a sweep
holds a push, cleared the moment a push actually lands.

Revision ID: bd78141d2722
Revises: f4e5d6c7b8a9
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bd78141d2722"
down_revision: Union[str, Sequence[str], None] = "f4e5d6c7b8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notification_sends",
        sa.Column("suppressed_reason", sa.String(length=40), nullable=True),
    )
    # Backs the re-fire cooldown lookup ("most recent resolved row for this
    # fingerprint") — without it that's a full-table scan of every resolved
    # episode ever recorded, on every sweep, for every fingerprint currently
    # firing.
    op.create_index(
        "ix_notification_sends_fingerprint_resolved_at",
        "notification_sends",
        ["fingerprint", "resolved_at"],
    )


def downgrade() -> None:
    # IF EXISTS throughout — a partially-applied upgrade must still be
    # reversible (same rule as every other migration in this package).
    op.execute("DROP INDEX IF EXISTS ix_notification_sends_fingerprint_resolved_at")
    op.execute(
        "ALTER TABLE notification_sends DROP COLUMN IF EXISTS suppressed_reason"
    )
