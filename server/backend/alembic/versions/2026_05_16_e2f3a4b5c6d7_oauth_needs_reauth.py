"""oauth_tokens: track needs_reauth_at for graceful token-revocation handling

When Google revokes a refresh token (Testing-mode 7-day expiry, user-side
account revocation, password change, scope drift), the credential refresh in
app/auth/oauth.py::get_credentials raises RefreshError('invalid_grant'). Until
this column existed there was no way for the auth code to flag "this token is
dead, stop retrying" — the scheduler would just hammer the same dead token
every 15 minutes and accumulate consecutive_failures forever.

needs_reauth_at is set the first time we see a hard-revocation; cleared on the
next successful OAuth code exchange. The scheduler short-circuits sync attempts
on tokens with this set, and the dashboard surfaces a re-auth banner.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "oauth_tokens",
        sa.Column("needs_reauth_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "oauth_tokens",
        sa.Column("needs_reauth_reason", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("oauth_tokens", "needs_reauth_reason")
    op.drop_column("oauth_tokens", "needs_reauth_at")
