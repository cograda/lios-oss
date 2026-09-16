"""household domains — Phase B of household-ops-and-loops-2026-08.md

Hand-written (no local Postgres/Docker available in this environment to run
`alembic revision --autogenerate`) following the shape of the most recent
migration (`2026_08_08_8b9c0d1e2f3a_notification_sends_user_id.py`) and the
`snags` initial-table migrations for the two-table pattern. Mirrors
`app/integrations/household/models.py` exactly: `domains` (household-shared,
single `owner_id` FK — no `UserOwnedMixin`) and `domain_checks` (append-only
self-report log).

No seed rows here — domains are created at runtime via `household_domain_add`,
never committed (`tests/test_personalisation_guard.py` would reject a
household name in a migration default).

`DROP ... IF EXISTS` throughout `downgrade()` per user-memory
`feedback_alembic_migration_safety.md` — a partially-applied upgrade must
still be reversible.

Revision ID: aab4759a66ce
Revises: 8b9c0d1e2f3a
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "aab4759a66ce"
down_revision: Union[str, Sequence[str], None] = "8b9c0d1e2f3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "domains",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("operational_definition", sa.Text(), nullable=False),
        sa.Column("scope_note", sa.Text(), nullable=False),
        sa.Column("cadence", sa.String(length=100), nullable=True),
        sa.Column(
            "checklist", postgresql.ARRAY(sa.Text()),
            nullable=False, server_default="{}",
        ),
        sa.Column("standard_note", sa.Text(), nullable=True),
        sa.Column("standard_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"],
            name="fk_domains_owner_id_users", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("name", name="uq_domains_name"),
    )
    op.create_index("ix_domains_name", "domains", ["name"])
    op.create_index("ix_domains_owner_id", "domains", ["owner_id"])

    op.create_table(
        "domain_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain_id", sa.Integer(), nullable=False),
        sa.Column("checked_by_id", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"], ["domains.id"],
            name="fk_domain_checks_domain_id_domains", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["checked_by_id"], ["users.id"],
            name="fk_domain_checks_checked_by_id_users", ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_domain_checks_domain_id", "domain_checks", ["domain_id"])
    op.create_index("ix_domain_checks_created_at", "domain_checks", ["created_at"])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_domain_checks_created_at")
    op.execute("DROP INDEX IF EXISTS ix_domain_checks_domain_id")
    op.execute("DROP TABLE IF EXISTS domain_checks")

    op.execute("DROP INDEX IF EXISTS ix_domains_owner_id")
    op.execute("DROP INDEX IF EXISTS ix_domains_name")
    op.execute("DROP TABLE IF EXISTS domains")
