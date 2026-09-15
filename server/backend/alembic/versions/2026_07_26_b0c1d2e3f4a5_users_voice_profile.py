"""users.voice_profile — per-user tone for instructions/briefing rendering.

sam-rollout D2. Two values: 'direct' (Alex, default — current behavior,
concise and direct) and 'curious' (Sam — warm/curious, patterns surfaced
as questions, never accusatory about missed tasks/streaks; her recorded
reaction to Alex's tuning was that it read as accusatory).

Added nullable, backfilled per known user, then made NOT NULL with a
server_default of 'direct' so any future user row inserted without an
explicit value still gets sane behavior rather than a constraint error.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_PROFILE = "direct"
_SAM_PROFILE = "curious"
_SAM_USER_ID = 2


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("voice_profile", sa.String(20), nullable=True),
    )
    op.execute(f"UPDATE users SET voice_profile = '{_DEFAULT_PROFILE}' WHERE voice_profile IS NULL")
    op.execute(
        f"UPDATE users SET voice_profile = '{_SAM_PROFILE}' WHERE id = {_SAM_USER_ID}"
    )
    op.alter_column(
        "users",
        "voice_profile",
        nullable=False,
        server_default=_DEFAULT_PROFILE,
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_voice_profile")
    op.drop_column("users", "voice_profile")
