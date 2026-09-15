"""intake_markers — the per-caller watermark for tasks_intake_candidates

`app/integrations/tasks/models.py::IntakeMarker` (S5 intake work, 2026-09-07).
`UserOwnedMixin` shape, but `user_id` is the primary key rather than a plain
column — there is exactly one marker per caller, ever, so a separate
surrogate id would be a second uniqueness constraint on the same value.

`ondelete="RESTRICT"` matches every other UserOwnedMixin table in this repo
(app/mixins.py::UserOwnedMixin's own docstring): deleting a user with data
must fail loudly rather than silently drop their marker.

The constraint drop is `IF EXISTS` before creation per the repo's standing
convention (see the 2026-09-07 tasks.kind/severity migration's docstring for
the crash-loop this guards against), even though this migration only adds a
new table — a half-applied prior attempt at this exact table is the case it
tolerates.

Revision ID: 846af90574fa
Revises: 99022513af93
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa

revision = "846af90574fa"
down_revision = "99022513af93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS intake_markers")
    op.create_table(
        "intake_markers",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("seen_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("intake_markers")
