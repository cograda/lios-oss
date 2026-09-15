"""algo harness — predictions, model versions, runs.

Three kernel-owned tables shared by every `type="deriver"` integration. Owned
by the kernel rather than by an integration because the manifest requires an
integration's models to live in its own models.py, so per-integration
ownership would mean one predictions table per algo — and then scoring,
backtesting and the dashboard would each need per-algo code. See
app/models/algo.py for the full reasoning.

Household-shared (no user_id), same as commute_decisions and ha_*: rows are
written by the unattended scheduler with no current_user_id() context.

The load-bearing constraint is uq_algo_predictions_target on
(algo, quantity, target_at, horizon_min). Many rows for one target moment at
different horizons is the point — it is what makes accuracy-by-horizon
answerable — while a retry or an APScheduler misfire catch-up re-running a
cycle must not stack duplicates at the same horizon. app/algo/predictions.py
names this constraint in an ON CONFLICT clause, so renaming it here breaks
recording at runtime rather than at import.

Revision ID: b3d9e5a1c7f2
Revises: f7a2c4e9b1d3
Create Date: 2026-08-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b3d9e5a1c7f2"
down_revision: Union[str, Sequence[str], None] = "f7a2c4e9b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "algo_model_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("algo", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("params", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("feature_names", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("trained_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("trained_rows", sa.Integer(), nullable=True),
        sa.Column("train_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("train_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("algo", "version", name="uq_algo_model_versions_algo_version"),
    )
    op.create_index("ix_algo_model_versions_algo", "algo_model_versions", ["algo"])
    op.create_index("ix_algo_model_versions_trained_at", "algo_model_versions", ["trained_at"])
    op.create_index("ix_algo_model_versions_algo_active", "algo_model_versions", ["algo", "is_active"])

    op.create_table(
        "algo_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("algo", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.String(length=64), nullable=False),
        sa.Column("made_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_min", sa.Integer(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("lower", sa.Float(), nullable=True),
        sa.Column("upper", sa.Float(), nullable=True),
        sa.Column("algo_version", sa.Integer(), nullable=True),
        sa.Column("features_hash", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("baseline", sa.Float(), nullable=True),
        sa.Column("actual", sa.Float(), nullable=True),
        sa.Column("error", sa.Float(), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "algo", "quantity", "target_at", "horizon_min",
            name="uq_algo_predictions_target",
        ),
    )
    op.create_index("ix_algo_predictions_algo", "algo_predictions", ["algo"])
    op.create_index("ix_algo_predictions_target_at", "algo_predictions", ["target_at"])
    op.create_index("ix_algo_predictions_run_id", "algo_predictions", ["run_id"])
    op.create_index("ix_algo_predictions_unscored", "algo_predictions", ["algo", "scored_at", "target_at"])
    op.create_index("ix_algo_predictions_made", "algo_predictions", ["algo", "made_at"])

    op.create_table(
        "algo_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("algo", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("n_predictions", sa.Integer(), nullable=True),
        sa.Column("n_scored", sa.Integer(), nullable=True),
        sa.Column("model_version", sa.Integer(), nullable=True),
        sa.Column("llm_model", sa.String(length=64), nullable=True),
        sa.Column("llm_calls", sa.Integer(), nullable=True),
        sa.Column("llm_tokens", sa.Integer(), nullable=True),
        sa.Column("llm_cost_usd", sa.Float(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), server_default="{}", nullable=False),
    )
    op.create_index("ix_algo_runs_algo", "algo_runs", ["algo"])
    op.create_index("ix_algo_runs_algo_started", "algo_runs", ["algo", "started_at"])


def downgrade() -> None:
    # DROP ... IF EXISTS throughout: a partially-applied upgrade (the second
    # create_table failing on a full disk, say) otherwise leaves a downgrade
    # that cannot run, which is the worst moment to discover it.
    op.execute("DROP INDEX IF EXISTS ix_algo_runs_algo_started")
    op.execute("DROP INDEX IF EXISTS ix_algo_runs_algo")
    op.execute("DROP TABLE IF EXISTS algo_runs")

    for idx in (
        "ix_algo_predictions_made",
        "ix_algo_predictions_unscored",
        "ix_algo_predictions_run_id",
        "ix_algo_predictions_target_at",
        "ix_algo_predictions_algo",
    ):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS algo_predictions")

    for idx in (
        "ix_algo_model_versions_algo_active",
        "ix_algo_model_versions_trained_at",
        "ix_algo_model_versions_algo",
    ):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS algo_model_versions")
